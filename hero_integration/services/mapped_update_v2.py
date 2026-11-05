"""Schema-bound, update-only writeback for approved native Query mappings.

Mapping JSON is configuration, never authority: source binding, read approval,
mutation contract, local permission, conflict proof and explicit confirmation are
independently rechecked before the single remote attempt.
"""
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
import re
from types import SimpleNamespace
from uuid import UUID
from zoneinfo import ZoneInfo

import frappe

from hero_integration.services import mapped_send as send_helpers
from hero_integration.services.localized_error import LocalizedError
from hero_integration.services.native_projections import _instant

USAGE = 'MAPPED_UPDATE_V2'
PATH = re.compile(r'^[_A-Za-z][_0-9A-Za-z]*(?:\.[_A-Za-z][_0-9A-Za-z]*)*$')
AREAS = {
 'HeroCustomers': ('Customer','customer','contacts','update_contact','contact.id'),
 'HeroProjectMatches': ('Project','project','project_matches','update_project_match','project_match.id'),
 'HeroCalendarEvents': ('Event','CalendarEvent','calendar_events','update_calendar_event','calendar_event.id'),
 'HeroTasks': ('Task','Task','tasks','update_task','task.id'),
 'HeroProducts': ('Item','supply_product','supply_product_versions','update_supply_product_version','supply_product_version.id'),
 'HeroServices': ('Item','supply_service','supply_services','update_supply_service','supply_service.id'),
 'HeroDocuments': ('File','file_upload','FileUpload_FileUploads','update_file_upload','id'),
 'HeroPictures': ('File','file_upload','FileUpload_FileUploads','update_file_upload','id'),
 'HeroEmployees': ('Employee','partner','partners','update_partner','partner.id'),
}
RESOLVERS = {'FIELD','PLAIN_TEXT','HTML','VALUE_MAP','DATETIME','EVENT_TIME','LINK','BARCODE','PRICE','MASTER_NAME','DATE','USER_LINK'}
FORBIDDEN = {'id','product_id','uuid','created','modified','is_deleted','deleted','delete','password','role','roles',
             'account_type','enabled','user_type','last_login','profile_image_uuid','signature','no_signature'}
MISSING = object()

class MappedUpdateError(LocalizedError):
    pass


class MappedUpdateBindingError(MappedUpdateError):
    """A native relationship needs review; no identity may be guessed."""


def _json(value):
    return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),default=str,allow_nan=False)


def _at(value,path,default=MISSING):
    for part in path.split('.'):
        if not isinstance(value,dict) or part not in value: return default
        value=value[part]
    return value


def _put(value,path,item):
    parts=path.split('.')
    for part in parts[:-1]:
        current=value.setdefault(part,{})
        if not isinstance(current,dict): raise MappedUpdateError('Mutation input paths overlap.')
        value=current
    if parts[-1] in value and _json(value[parts[-1]]) != _json(item):
        raise MappedUpdateError('Mutation input paths overlap.')
    value[parts[-1]]=item


def _schema(raw):
    from graphql import build_client_schema
    from hero_integration.services.graphql_graph import normalize_schema
    return build_client_schema({'__schema':normalize_schema(raw)})


def _input_type(schema,operation,path):
    from graphql import get_named_type, is_input_object_type, is_list_type, is_non_null_type
    root=schema.mutation_type.fields.get(operation) if schema.mutation_type else None
    parts=path.split('.')
    if root is None or parts[0] not in root.args: raise MappedUpdateError('Mutation input path is absent from the saved schema.')
    selected=root.args[parts[0]].type
    for part in parts[1:]:
        unwrapped=selected.of_type if is_non_null_type(selected) else selected
        owner=get_named_type(selected)
        if is_list_type(unwrapped) or not is_input_object_type(owner) or part not in owner.fields:
            raise MappedUpdateError('Mutation input path is not a scalar object member.')
        selected=owner.fields[part].type
    return selected


def _typed(value,kind):
    from graphql import coerce_input_value, GraphQLError
    if isinstance(value,Decimal): value=float(value)
    try: return coerce_input_value(value,kind)
    except GraphQLError as error: raise MappedUpdateError('A configured value is incompatible with its HERO input type.') from error


def _read_type(raw,area,path):
    from hero_integration.services.query_mapping import source_type
    prefix='edges.node.' if area in {'HeroDocuments','HeroPictures'} else ''
    operation='HeroEmployees' if area=='HeroEmployees' else AREAS[area][2]
    return source_type(raw,operation,prefix+path)


def _copy_allowed(area,path,read_path):
    allowed={
      'HeroCustomers': {'contact.address.id':'address.id'},
      'HeroProducts': {'supply_product_version.base_data.id':'base_data.id',
          'supply_product_version.base_data.product_id':'base_data.product_id',
          'supply_product_version.product_id':'product_id'},
      'HeroEmployees': {'partner.address.id':'address.id','partner.first_name':'first_name','partner.last_name':'last_name','partner.email':'email'},
    }
    return allowed.get(area,{}).get(path)==read_path


def normalise_mapping(content,raw_schema):
    """Validate the entire closed configuration without any database or remote I/O."""
    from graphql import get_named_type,is_scalar_type,is_enum_type,is_list_type,is_non_null_type
    from hero_integration.services.personal_mapping_content import bounded
    bounded(content)
    required={'usage','version','area','read_service','source','target','identity','assignments','required_readback'}
    if not isinstance(content,dict) or set(content)!=required or content.get('usage')!=USAGE or type(content.get('version')) is not int or content['version']!=2:
        raise MappedUpdateError('Invalid mapped update configuration.')
    value=deepcopy(content);area=value['area']
    if not isinstance(area,str) or area not in AREAS: raise MappedUpdateError('Unsupported mapped update area.')
    owner,_kind,_root,operation,identity_path=AREAS[area]
    if value['source']!={'doctype':owner} or value['target']!={'operation':operation}:
        raise MappedUpdateError('Mapped update source and operation namespaces differ.')
    if not isinstance(value['read_service'],str) or not 0<len(value['read_service'])<=140:
        raise MappedUpdateError('Mapped update requires an explicit approved read service.')
    identity=value['identity']
    if (not isinstance(identity,dict) or set(identity)!={'read_path','input_path','mutation_read_path','response_path'}
            or any(not isinstance(v,str) or not PATH.fullmatch(v) for v in identity.values())
            or identity['input_path']!=identity_path or identity['mutation_read_path']!='id' or identity['response_path']!='id'):
        raise MappedUpdateError('Mutation identity must remain server-owned and distinct from source identity.')
    valid_read={'product_id'} if area=='HeroProducts' else {'id','uuid'} if owner=='File' else {'id'}
    if identity['read_path'] not in valid_read: raise MappedUpdateError('Read identity is outside this native namespace.')
    schema=_schema(raw_schema)
    for path in (identity['read_path'],identity['mutation_read_path']): _read_type(raw_schema,area,path)
    id_type=_input_type(schema,operation,identity_path)
    if str(get_named_type(id_type)) not in {'Int','ID'}: raise MappedUpdateError('Mutation identity must use its verified Int or ID input.')
    assignments=value['assignments'];copies=value['required_readback']
    if not isinstance(assignments,list) or not assignments or len(assignments)>100 or not isinstance(copies,list) or len(copies)>20:
        raise MappedUpdateError('Mapped updates require a bounded list of explicit assignments.')
    seen_ids=set();seen_sources=set();seen_inputs=set()
    for item in assignments:
        keys={'id','source','input_path','read_path','query_rule_id','resolver','options'}
        if not isinstance(item,dict) or set(item)!=keys: raise MappedUpdateError('Invalid mapped update assignment.')
        try:
            if str(UUID(item['id']))!=item['id'] or item['id'] in seen_ids: raise ValueError
        except (ValueError,TypeError,AttributeError): raise MappedUpdateError('Assignment identities must be unique canonical UUIDs.') from None
        seen_ids.add(item['id'])
        source=item['source'];path=item['input_path'];opts=item['options']
        if (not isinstance(source,dict) or set(source)!={'doctype','field'} or any(not isinstance(v,str) or not v for v in source.values())
                or not isinstance(path,str) or not PATH.fullmatch(path) or not PATH.fullmatch(item['read_path'])
                or not isinstance(item['query_rule_id'],str) or len(item['query_rule_id'])>140
                or item['resolver'] not in RESOLVERS or not isinstance(opts,dict)
                or set(opts)-{'map','price_list','timezone','when','query_path','missing','null'}):
            raise MappedUpdateError('Invalid mapped update source, path or resolver.')
        from hero_integration.services.project_naming import is_local_project_name
        if is_local_project_name(source, path):
            raise MappedUpdateError('Project naming is local to ERPNext and cannot be sent to HERO.')
        allowed={owner}|({'Contact','Address'} if owner=='Customer' else {'Item Price'} if owner=='Item' else {'User'} if owner=='Employee' else {'ToDo'} if owner=='Task' else set())
        if source['doctype'] not in allowed: raise MappedUpdateError('Source is outside the verified native relations.')
        if path in seen_inputs or any(path.startswith(p+'.') or p.startswith(path+'.') for p in seen_inputs):
            raise MappedUpdateError('Mutation input paths overlap.')
        seen_inputs.add(path)
        relative=path.split('.',1)[1] if '.' in path else path
        default_price=area=='HeroProducts' and relative=='default_sales_price' and item['read_path']=='sales_prices' and item['resolver']=='PRICE'
        if relative!=item['read_path'] and not default_price:
            raise MappedUpdateError('Mutation input and verified readback must refer to the same HERO field.')
        sourcekey=(source['doctype'],source['field'],opts.get('price_list',''))
        if sourcekey in seen_sources: raise MappedUpdateError('A source field must have one unambiguous mutation target.')
        seen_sources.add(sourcekey)
        leaf=path.split('.')[-1]
        if (leaf in FORBIDDEN and not (area=='HeroEmployees' and path=='partner.signature' and source=={'doctype':'User','field':'email_signature'}) or area=='HeroEmployees' and path=='partner.status' or path==identity_path or any(part in {'current_project_match_status','user'} for part in path.split('.')[:-1])
                and not (area=='HeroEmployees' and path=='partner.user.locale') or source['field'] in {'name','item_code','user_id','email','employee_name'}):
            raise MappedUpdateError('Identity, account, lifecycle and observed system fields cannot be reversed.')
        if leaf.endswith('_id') and item['resolver'] not in {'LINK','USER_LINK'}: raise MappedUpdateError('Relationship inputs require an explicit verified Link resolver.')
        if item['resolver']=='LINK' and (area,path,source['doctype'],source['field']) not in {
             ('HeroProjectMatches','project_match.customer_id','Project','customer'),
             ('HeroCalendarEvents','calendar_event.project_match_id','Event','custom_hero_project'),
             ('HeroTasks','task.target_project_match_id','Task','project')}:
            raise MappedUpdateError('The relationship resolver is outside its reviewed identity namespace.')
        if item['resolver']=='USER_LINK' and (area,path,source['doctype'],source['field'])!=('HeroTasks','task.target_user_id','ToDo','allocated_to'):
            raise MappedUpdateError('The relationship resolver is outside its reviewed identity namespace.')
        if item['resolver']=='PRICE' and (owner!='Item' or source!= {'doctype':'Item Price','field':'price_list_rate'} or not isinstance(opts.get('price_list'),str) or not opts['price_list'].strip() or len(opts['price_list'])>140):
            raise MappedUpdateError('Price updates require an explicit managed native price list.')
        if source['doctype']=='Item Price' and item['resolver']!='PRICE': raise MappedUpdateError('Item Price requires the managed-price resolver.')
        if item['resolver']=='BARCODE' and (owner!='Item' or source['field']!='barcodes'):
            raise MappedUpdateError('Barcode selection requires the native Item barcode table.')
        if item['resolver']=='EVENT_TIME' and (area!='HeroCalendarEvents' or source['field'] not in {'starts_on','ends_on'}):
            raise MappedUpdateError('Event time conversion requires a native Event time field.')
        kind=_input_type(schema,operation,path);named=get_named_type(kind)
        if not (is_scalar_type(named) or is_enum_type(named)) or is_list_type(kind.of_type if is_non_null_type(kind) else kind):
            raise MappedUpdateError('Updates require scalar inputs; relationship lists need a separate reviewed adapter.')
        _read_type(raw_schema,area,item['read_path'])
        _read_type(raw_schema,area,opts.get('query_path',item['read_path']))
        if str(named)=='DateTime' and item['resolver'] not in {'DATETIME','EVENT_TIME'} or str(named)=='Date' and item['resolver']!='DATE':
            raise MappedUpdateError('Temporal mutation inputs require an explicit timezone resolver.')
        if item['resolver']=='DATETIME' and not opts.get('timezone'):
            raise MappedUpdateError('Datetime updates require an explicit IANA timezone.')
        if item['resolver']=='VALUE_MAP':
            mapping=opts.get('map')
            if not isinstance(mapping,dict) or not mapping or len(mapping)>100 or any(not isinstance(k,str) for k in mapping):
                raise MappedUpdateError('Value Map requires explicit scalar pairs.')
            for choice in mapping.values(): _typed(choice,kind)
        elif 'map' in opts: raise MappedUpdateError('Only Value Map accepts a conversion table.')
        if 'timezone' in opts: ZoneInfo(opts['timezone'])
        when=opts.get('when')
        if when is not None and (not isinstance(when,dict) or set(when)!={'field','equals'} or not isinstance(when['field'],str) or not PATH.fullmatch(when['field']) or isinstance(when['equals'],(dict,list))):
            raise MappedUpdateError('A source condition must be an explicit local-field equality.')
        if opts.get('missing','ERROR') not in {'ERROR','OMIT'} or opts.get('null','OMIT') not in {'OMIT','EXPLICIT_NULL','EMPTY_STRING'}:
            raise MappedUpdateError('Missing and null value policies must be explicit.')
    copy_paths=set()
    for item in copies:
        if not isinstance(item,dict) or set(item)!={'input_path','read_path'} or not _copy_allowed(area,item['input_path'],item['read_path']) or item['input_path'] in copy_paths:
            raise MappedUpdateError('Required readback fields cannot grant arbitrary mutation inputs.')
        copy_paths.add(item['input_path']);_input_type(schema,operation,item['input_path']);_read_type(raw_schema,area,item['read_path'])
    mandatory={'supply_product_version.base_data.id','supply_product_version.base_data.product_id','supply_product_version.product_id'} if area=='HeroProducts' else {'partner.first_name','partner.last_name','partner.email'} if area=='HeroEmployees' else set()
    if not mandatory<=copy_paths: raise MappedUpdateError('Required unchanged input members must have proven readback bindings.')
    if any(a['source']['doctype']=='Address' for a in assignments) and 'contact.address.id' not in copy_paths:
        raise MappedUpdateError('Address changes require a server-owned related address identity.')
    if area=='HeroEmployees' and any(a['input_path'].startswith('partner.address.') for a in assignments) and 'partner.address.id' not in copy_paths:
        raise MappedUpdateError('Address changes require a server-owned related address identity.')
    return value


def validate_sources(content, *, field_provider=None):
    """Check effective metadata and native permissions, never relax protected fields."""
    def field_for(doctype, name):
        field = field_provider(doctype, name) if field_provider else frappe.get_meta(doctype).get_field(name)
        return frappe._dict(vars(field)) if field is not None and not hasattr(field, 'get') else field
    for item in content['assignments']:
        source=item['source'];field=field_for(source['doctype'], source['field'])
        contact_proxy=(source['doctype']=='Contact' and source['field'] in {'email_id','mobile_no','phone'}
            or source=={'doctype':'File','field':'file_name'})
        if (field is None or field.get('hidden') or field.get('is_virtual') or field.get('permlevel')
                or field.get('read_only') and not contact_proxy or field.fieldtype in {'Password','Code','Attach','Attach Image','Signature'}
                or field.fieldtype in {'Table','Table MultiSelect'} and item['resolver']!='BARCODE'):
            raise MappedUpdateError('The mapped native source is protected, absent or not editable.')
        if field.fieldtype=='Link' and item['resolver'] not in {'LINK','MASTER_NAME','VALUE_MAP','USER_LINK'}:
            raise MappedUpdateError('A native Link source requires a reviewed identity or explicit value resolver.')
        condition=item['options'].get('when')
        if condition:
            control=field_for(source['doctype'], condition['field'])
            if control is None or control.get('hidden') or control.get('permlevel') or control.fieldtype=='Password':
                raise MappedUpdateError('A source condition must be an explicit local-field equality.')
        if item['resolver']=='MASTER_NAME' and (source['doctype'],source['field'],field.options) not in {
             ('Item','brand','Brand'),('Item','stock_uom','UOM'),('Item','item_group','Item Group')}:
            raise MappedUpdateError('Only reviewed Item master names may map to textual HERO master fields.')


def _link(doctype,name,source,kind):
    rows=frappe.get_all('HERO Integration Link',filters={'source_system':'HERO','source_instance':source,
        'target_doctype':doctype,'target_name':name,'source_type':kind},pluck='name',limit_page_length=2)
    if len(rows)!=1: raise MappedUpdateBindingError('Exactly one native source binding is required.')
    link=frappe.get_doc('HERO Integration Link',rows[0])
    if link.sync_status!='Synchronized' or getattr(link,'observed_source_hash',None) and link.observed_source_hash!=link.source_payload_hash:
        raise MappedUpdateBindingError('The native binding requires synchronization or conflict review.')
    return link



def _validate_contract_scope(mapping, content, contract):
    """An active operation contract only authorizes its explicit portable scope."""
    from hero_integration.services.config_mutation_contracts import _scope
    from hero_integration.services.personal_mapping_content import unique_json_object
    try:
        scope = _scope(json.loads(contract.mapping_json, object_pairs_hook=unique_json_object),
                       mapping.operation_name)
        portable = getattr(mapping, 'portable_definition_id', None)
        if portable not in (None, ''):
            if not isinstance(portable, str) or str(UUID(portable)) != portable:
                raise ValueError
            identity = portable
        else:
            raw = getattr(mapping, 'definition_id', None)
            if not isinstance(raw, str) or not (re.fullmatch(r'[0-9a-f]{32}', raw)
                                               or str(UUID(raw)) == raw):
                raise ValueError
            identity = str(UUID(raw))
        if content['area'] not in scope['areas'] or identity not in scope['mapping_definition_ids']:
            raise ValueError
    except (ValueError, TypeError, AttributeError, KeyError) as error:
        raise MappedUpdateError('The active mutation contract does not authorize this mapping identity and area.') from error


def _load_context(mapping_name,doctype,name):
    from hero_integration.client.settings import get_configuration
    from hero_integration.services.writeback_source import capture_source
    from hero_integration.services.graphql_contract import parse_mutation
    if not send_helpers._roles(): raise MappedUpdateError('Only System Managers and HERO Writeback users may send to HERO.')
    mapping=frappe.get_doc('HERO GraphQL Mapping',mapping_name);mapping.check_permission('read')
    snapshot=frappe.get_doc('HERO GraphQL Schema Snapshot',mapping.schema_snapshot);snapshot.check_permission('read')
    config=get_configuration(fresh=True)
    if (mapping.status!='Active' or mapping.direction!='FRAPPE_TO_MUTATION' or mapping.operation_kind!='MUTATION'
            or snapshot.analysis_status!='Validated' or getattr(snapshot,'execution_contract_status','')=='Blocked'
            or mapping.schema_fingerprint!=snapshot.schema_fingerprint or mapping.source_instance!=config.source_instance):
        raise MappedUpdateError('The active mapping, snapshot and confirmed source no longer agree.')
    content=normalise_mapping(json.loads(mapping.mapping_json),json.loads(snapshot.snapshot_json));validate_sources(content)
    if doctype!=content['source']['doctype'] or mapping.operation_name!=content['target']['operation']:
        raise MappedUpdateError('The selected document is outside this mapping namespace.')
    document=frappe.get_doc(doctype,name);frappe.has_permission(doctype,'write',doc=document,throw=True)
    proof=capture_source(snapshot,config.source_instance,config)
    link=_link(doctype,name,proof.source_instance,AREAS[content['area']][1])
    read=send_helpers._load_read(content['read_service'],snapshot)
    if read.contract.target_operation!=content['area'] or read.contract.identity_path!=content['identity']['read_path']:
        raise MappedUpdateError('Approved readback and native source identity differ.')
    contracts=frappe.get_all('HERO Mutation Contract',filters={'schema_snapshot':snapshot.name,
        'operation_name':mapping.operation_name,'status':'Active'},pluck='name')
    if len(contracts)!=1: raise MappedUpdateError('Exactly one active schema-bound mutation contract is required.')
    contract=frappe.get_doc('HERO Mutation Contract',contracts[0])
    _validate_contract_scope(mapping,content,contract)
    service=frappe.get_doc('HERO GraphQL Service',contract.graphql_service)
    parsed=parse_mutation(service.query_document)
    if (service.status!='Validated' or service.schema_snapshot!=snapshot.name or service.schema_fingerprint!=snapshot.schema_fingerprint
            or contract.schema_fingerprint!=snapshot.schema_fingerprint or contract.contract_fingerprint!=parsed.fingerprint
            or parsed.root_field!=mapping.operation_name):
        raise MappedUpdateError('The approved mutation contract or service changed.')
    ctx=SimpleNamespace(document=document,mapping=mapping,snapshot=snapshot,content=content,proof=proof,link=link,
        read=read,contract=contract,service=service,parsed=parsed,related={},source_links={},source_documents={},
        query_plan=None,raw_schema=json.loads(snapshot.snapshot_json),hero_id=str(link.source_id))
    from hero_integration.services.migration import _mapping_plan_for_run
    ctx.query_plan=_mapping_plan_for_run(SimpleNamespace(graphql_service=read.service.name,graphql_mapping=read.mapping.name,
        service_revision=read.contract.service_revision,mapping_revision=read.contract.mapping_revision),content['area'])
    return ctx


def _read_record(ctx,client):
    """Stream the approved query without accumulating all article records in memory."""
    from hero_integration.sources.hero import GraphQLServiceSource
    from hero_integration.services.read_contract import from_documents
    from hero_integration.services.writeback_source import validate_source
    def revalidate():
        service=frappe.get_doc('HERO GraphQL Service',ctx.read.service.name)
        mapping=frappe.get_doc('HERO GraphQL Mapping',ctx.read.mapping.name)
        contract=from_documents(service,mapping,ctx.snapshot)
        if contract!=ctx.read.contract or service.query_document!=ctx.read.service.query_document:
            raise MappedUpdateError('The approved readback query changed.')
        return contract
    if ctx.content['area'] == 'HeroProjectMatches':
        from hero_integration.services.project_identity_read import ProjectIdentityReader
        client = ProjectIdentityReader(client, ctx.raw_schema, ctx.hero_id)
    source=GraphQLServiceSource(client,ctx.read.service,ctx.read.contract,ctx.proof.source_instance,revalidate=revalidate)
    found=None;offset=0
    for _ in range(2000):
        validate_source(ctx.proof);page=source._page(offset,ctx.content['area'])
        for row in page.records:
            identity=_at(row,ctx.content['identity']['read_path'])
            if identity is MISSING: raise MappedUpdateError('The approved readback omitted its identity.')
            if str(identity)==ctx.hero_id:
                if found is not None: raise MappedUpdateError('Readback identity is ambiguous.')
                found=row
        if page.next_offset is None or ctx.content['area'] == 'HeroProjectMatches':
            if found is None: raise MappedUpdateError('The bound HERO record was not found in the approved readback.')
            _guard_record(ctx,found);return found
        if not page.records or page.next_offset<=offset: raise MappedUpdateError('Readback pagination did not advance.')
        offset=page.next_offset
    raise MappedUpdateError('Approved readback exceeded the bounded page limit.')


def _guard_record(ctx,record):
    area=ctx.content['area']
    if record.get('is_deleted') is True or record.get('deleted') is True:
        raise MappedUpdateError('A deleted HERO record cannot be updated.')
    if area=='HeroCalendarEvents' and (record.get('readonly') is not False or record.get('is_recurring') is not False):
        raise MappedUpdateError('Readonly or recurring HERO events require their separate provider/recurrence workflow.')
    if area in {'HeroDocuments','HeroPictures'} and (record.get('category')=='images') != (area=='HeroPictures'):
        raise MappedUpdateError('The source file category differs from the mapped native area.')
    if area=='HeroProducts' and (not isinstance(record.get('id'),int) or record['id']<=0
            or not isinstance(_at(record,'base_data.id'),int) or _at(record,'base_data.id')<=0
            or str(_at(record,'base_data.product_id'))!=str(record.get('product_id'))):
        raise MappedUpdateError('Product version and base-data identities were not proven by the readback.')
    if area=='HeroEmployees' and (record.get('user_id')!=_at(record,'user.id') or type(record.get('user_id')) is not int):
        raise MappedUpdateError('Partner and User identities do not match in the saved readback.')


def _source(ctx,item,remote):
    source=item['source'];dt=source['doctype'];field=source['field'];owner=ctx.document.doctype
    if dt==owner: doc=ctx.document;link=ctx.link
    elif owner=='Customer' and dt in {'Contact','Address'}:
        doc=send_helpers._source_document(ctx,dt)
        if dt=='Address':
            link=_link(dt,doc.name,ctx.proof.source_instance,'customer_address')
            if str(link.source_id)!=str(_at(remote,'address.id')): raise MappedUpdateError('The related Address identity differs from HERO.')
        else: link=ctx.link
    elif owner=='Task' and dt=='ToDo':
        doc=send_helpers._source_document(ctx,dt);link=ctx.link
    elif owner=='Employee' and dt=='User':
        user=ctx.document.get('user_id')
        if not user or user.lower() in {'administrator','guest'}: raise MappedUpdateError('The employee has no safe native User relation.')
        doc=frappe.get_doc('User',user);link=_link('User',user,ctx.proof.source_instance,'User')
        if str(link.source_id)!=str(remote.get('user_id')): raise MappedUpdateError('The native User does not match the partner user identity.')
        others=frappe.get_all('Employee',filters={'user_id':user},pluck='name',limit_page_length=2)
        if others!=[ctx.document.name]: raise MappedUpdateError('The employee User relation is not unique.')
    elif owner=='Item' and dt=='Item Price':
        from hero_integration.services.product_migration import _DIMENSIONS
        collection=_price_collection(ctx,item)
        if collection:
            from hero_integration.services.mapped_collection_relations import lookup_managed_price
            managed=lookup_managed_price(ctx.document.name,item['options']['price_list'],ctx.proof.source_instance,allow_rate_change=True)
            doc,link=managed['document'],managed['link']
            if link.sync_status!='Synchronized' or getattr(link,'observed_source_hash',None) and link.observed_source_hash!=link.source_payload_hash:
                raise MappedUpdateError('The managed sales price requires conflict review.')
            ctx.price_collections=getattr(ctx,'price_collections',{})
            ctx.price_collections[item['id']]={**managed,'config':collection}
            _collection_remote(ctx,item,remote)
            ctx.source_documents[item['id']]=doc;ctx.source_links[item['id']]=link
            return doc,doc.get(field)
        baseline=json.loads(ctx.link.target_field_hashes or '{}')
        managed=baseline.get('prices',{}).get(item['options']['price_list']) if ctx.content['area']=='HeroProducts' else baseline.get('price')
        if not isinstance(managed,dict) or not managed.get('name'): raise MappedUpdateError('The imported price has no managed baseline identity.')
        doc=frappe.get_doc('Item Price',managed['name']);link=ctx.link
        price_list=frappe.get_doc('Price List',item['options']['price_list']);frappe.has_permission('Price List','read',doc=price_list,throw=True)
        if (doc.item_code!=ctx.document.name or doc.price_list!=price_list.name or not price_list.enabled
                or doc.currency!=price_list.currency or (doc.uom or '')!=(managed.get('uom') or '')
                or doc.uom!=ctx.document.stock_uom or any(doc.get(key) for key in _DIMENSIONS)
                or doc.get('valid_from') and str(doc.valid_from)>str(frappe.utils.today())):
            raise MappedUpdateError('The managed price dimensions, currency or unit changed.')
    else: raise MappedUpdateError('Unsupported native source relation.')
    frappe.has_permission(dt,'read' if dt!=owner else 'write',doc=doc,throw=True)
    ctx.source_documents[item['id']]=doc;ctx.source_links[item['id']]=link
    return doc,doc.get(field)


def _price_collection(ctx,item):
    if ctx.content['area']!='HeroProducts' or item['resolver']!='PRICE' or not item['input_path'].endswith('.default_sales_price'): return None
    relations=(getattr(ctx.query_plan,'adapter_options',None) or {}).get('native_relations',[])
    selected=[r for r in relations if r.get('kind')=='sales_prices' and r.get('collection_path')==item['read_path']
        and any(v.get('name')==item['options']['price_list'] for v in r.get('price_lists',{}).values())]
    if len(selected)>1: raise MappedUpdateError('Managed sales-price collection binding is ambiguous.')
    return selected[0] if selected else None


def _collection_remote(ctx,item,remote):
    selected=ctx.price_collections[item['id']];config=selected['config'];rows=_at(remote,config['collection_path'])
    if not isinstance(rows,list): raise MappedUpdateError('Approved readback omitted its managed sales-price collection.')
    matches=[r for r in rows if isinstance(r,dict) and str(_at(r,config['identity_path']))==str(selected['source_id'])]
    if len(matches)!=1: raise MappedUpdateError('Managed sales-price row identity is not unique in approved readback.')
    row=matches[0];list_id=str(_at(row,config['price_list_identity_path']));binding=config['price_lists'].get(list_id)
    if (not binding or binding.get('name')!=item['options']['price_list']
            or binding.get('currency')!=selected['document'].currency
            or _at(row,'supply_sales_price.is_default') is not True):
        raise MappedUpdateError('Only the proven default HERO sales-price list can use the scalar default-price update.')
    value=_at(row,config['fields']['price_list_rate'])
    if type(value) not in (int,float) or value<0: raise MappedUpdateError('The managed remote sales price is not a nonnegative number.')
    return value


def _rule(ctx,item):
    source=item['source'];path=item['options'].get('query_path',item['read_path'])
    targets={(source['doctype'],source['field'])}
    if ctx.document.doctype=='Customer' and source['doctype']=='Contact' and source['field'] in {'first_name','last_name','email_id','mobile_no','salutation'}:
        targets.add(('Customer',source['field']))
    if source=={'doctype':'Contact','field':'phone'}: targets.add(('Contact Phone','phone'))
    found=[rule for rule in ctx.query_plan.definitions if (rule.target_doctype,rule.target_field) in targets and rule.source_path==path
        and (not item['query_rule_id'] or item['query_rule_id']==rule.definition_id)
        and (source['doctype']!='Item Price' or rule.relation==item['options'].get('price_list'))]
    if len(found)!=1: raise MappedUpdateError('Each outgoing field requires one matching approved Query binding.')
    return found[0]


def _canonical(ctx,item,remote):
    if item['id'] in getattr(ctx,'price_collections',{}): return _collection_remote(ctx,item,remote)
    from hero_integration.services.direct_mapping import transformed_value
    rule=_rule(ctx,item);raw=_at(remote,rule.source_path)
    if rule.transformation in {'Coalesce','Customer Display Name'}:
        from hero_integration.services.query_mapping_extensions import source_value
        raw=source_value(rule,remote,ctx.query_plan.adapter_options['transform_options'][rule.definition_id])
    if raw is MISSING: raise MappedUpdateError('The approved readback omitted a canonical field.')
    if item['resolver']=='USER_LINK':
        from hero_integration.services.query_mapping_extensions import bound_user
        return bound_user(raw,ctx.proof.source_instance)
    if rule.transformation in {'HERO Customer Link','HERO Project Link'} or item['resolver']=='LINK':
        if raw is None: return None
        target='Customer' if item['input_path'].endswith('.customer_id') else 'Project';kind='customer' if target=='Customer' else 'project'
        names=frappe.get_all('HERO Integration Link',filters={'source_system':'HERO','source_instance':ctx.proof.source_instance,
            'source_type':kind,'source_id':str(raw),'target_doctype':target,'sync_status':'Synchronized'},pluck='target_name',limit_page_length=2)
        if len(names)!=1: raise MappedUpdateError('The remote relation has no unique native identity.')
        return names[0]
    if ctx.content['area']=='HeroEmployees' and item['source']=={'doctype':'User','field':'language'}:
        from hero_integration.services.employee_mapping import preview as employee_preview
        result=employee_preview(remote,languages=frappe.get_all('Language',pluck='name'),plan=ctx.query_plan)
        if result['blockers']: raise MappedUpdateError('Employee canonical values require review.')
        return next(r['value'] for r in result['fields'] if (r['target_doctype'],r['target_field'])==('User','language'))
    if rule.transformation=='Native Adapter':
        if ctx.content['area']=='HeroTasks':
            return send_helpers._baseline_remote(ctx,{'source':item['source']},{'read_path':rule.source_path},raw,record=remote)
        if ctx.content['area']=='HeroCalendarEvents':
            from hero_integration.services.native_projections import values
            options=json.loads(ctx.read.mapping.mapping_json).get('native_options')
            return values({'native_kind':'event'},remote,ctx.hero_id,ctx.proof.source_instance,options,
                lookup=lambda *_args:None,allow_partial=False)[rule.target_field]
        raise MappedUpdateError('This native projection lacks a reversible baseline.')
    if getattr(ctx.query_plan,'adapter_options',None) is not None:
        from hero_integration.services.query_mapping import transform
        value=transform(rule,raw,ctx.query_plan.adapter_options,source_instance=ctx.proof.source_instance)
    else: value=transformed_value(rule,raw,source_instance=ctx.proof.source_instance)
    if ctx.content['area']=='HeroCustomers' and item['source']['field'] in {'mobile_no','phone'}:
        from hero_integration.services.migration import _phone_number
        value=_phone_number(value)[0]
    return value



def _unchanged_project_customer(ctx, item, value, remote):
    """A payload change on a Customer does not change an existing Project relation.

    This exception never authorizes a changed relationship or a Customer write.
    The strict root/source lookup remains the default everywhere else.
    """
    if (ctx.content.get('area') != 'HeroProjectMatches'
            or item['source'] != {'doctype': 'Project', 'field': 'customer'}
            or item['input_path'] != 'project_match.customer_id' or item['read_path'] != 'customer_id'
            or item['resolver'] != 'LINK'):
        return None
    rows = frappe.get_all('HERO Integration Link', filters={'source_system': 'HERO',
        'source_instance': ctx.proof.source_instance, 'target_doctype': 'Customer',
        'target_name': value, 'source_type': 'customer'}, pluck='name', limit_page_length=2)
    if len(rows) != 1: return None
    link = frappe.get_doc('HERO Integration Link', rows[0])
    identity = _at(remote, 'customer_id')
    if (link.sync_status != 'Synchronized' or type(identity) is not int or identity <= 0
            or str(link.source_id) != str(identity)):
        return None
    return link


def _native_project_notes(ctx, item):
    if (ctx.content.get('area') != 'HeroProjectMatches'
            or item['source'] != {'doctype': 'Project', 'field': 'notes'}
            or item['input_path'] != 'project_match.partner_notes' or item['read_path'] != 'partner_notes'
            or item['resolver'] != 'PLAIN_TEXT'):
        return False
    from hero_integration.services.direct_mapping import NOTES_BINDING, LEGACY_NOTES_BINDING
    rule = _rule(ctx, item)
    if ((rule.source_path, rule.target_doctype, rule.target_field) != NOTES_BINDING[:3]
            or rule.transformation not in {NOTES_BINDING[3], 'Safe HTML'}):
        return False
    # An active separate legacy rule still owns its own baseline.
    return not any(rule.target_doctype == 'Project' and rule.target_field == LEGACY_NOTES_BINDING[2]
                   for rule in ctx.query_plan.definitions)


def _project_notes_baseline(ctx, item, remote, canonical, source, target):
    """Compare each acknowledged representation with its historical transform."""
    from hero_integration.services.direct_mapping import LEGACY_NOTES_BINDING
    legacy = LEGACY_NOTES_BINDING[2]
    present = False
    for key, value in (('notes', canonical), (legacy, remote.get('partner_notes'))):
        if key not in source and key not in target: continue
        present = True
        if key == legacy:
            if value is not None and not isinstance(value, str): return False
            value = value.strip() if isinstance(value, str) and value.strip() else None
        if key not in source or key not in target or source[key] != send_helpers._hash(value):
            return False
    return present


def _wire(ctx,item,doc,value,remote):
    resolver=item['resolver'];opts=item['options']
    if value is None or value=='':
        policy=opts.get('null','OMIT')
        return MISSING if policy=='OMIT' else '' if policy=='EMPTY_STRING' else None
    if resolver=='DATE':
        if isinstance(value,datetime): raise MappedUpdateError('A configured value is incompatible with its HERO input type.')
        return (value if isinstance(value,date) else date.fromisoformat(str(value))).isoformat()
    if resolver=='USER_LINK':
        user=frappe.get_doc('User',value);frappe.has_permission('User','read',doc=user,throw=True)
        if not user.enabled or user.name.lower() in {'administrator','guest'}:
            raise MappedUpdateError('The related HERO identity is invalid.')
        rows=frappe.get_all('HERO Object Binding',filters={'site_scope':frappe.local.site,'source_instance':ctx.proof.source_instance,
            'object_type':'User','target_doctype':'User','target_name':user.name,'lifecycle':'VALID'},pluck='external_object_id',limit_page_length=2)
        if len(rows)!=1 or not str(rows[0]).isdigit() or int(rows[0])<=0:
            raise MappedUpdateError('The related HERO identity is invalid.')
        return int(rows[0])
    if resolver=='PRICE':
        import math
        if type(value) not in (int,float,Decimal) or not math.isfinite(value) or value<0:
            raise MappedUpdateError('A configured value is incompatible with its HERO input type.')
        return float(value)
    if resolver=='PLAIN_TEXT':
        from hero_integration.services.task_send import plain_text
        return plain_text(value)
    if resolver=='HTML':
        from frappe.utils import sanitize_html
        return sanitize_html(value)
    if resolver=='VALUE_MAP':
        if str(value) not in opts['map']: raise MappedUpdateError('The local value has no explicit reverse mapping.')
        return opts['map'][str(value)]
    if resolver=='BARCODE':
        values=[row.barcode for row in value if row.barcode]
        if len(values)!=1: raise MappedUpdateError('A single native barcode must be selected unambiguously.')
        return values[0]
    if resolver=='MASTER_NAME':
        field=frappe.get_meta(doc.doctype).get_field(item['source']['field']);linked=frappe.get_doc(field.options,value)
        frappe.has_permission(field.options,'read',doc=linked,throw=True)
        return linked.name
    if resolver=='LINK':
        field=frappe.get_meta(doc.doctype).get_field(item['source']['field']);target=field.options
        linked=frappe.get_doc(target,value);frappe.has_permission(target,'read',doc=linked,throw=True)
        try:
            link=_link(target,value,ctx.proof.source_instance,'customer' if target=='Customer' else 'project')
        except MappedUpdateBindingError:
            link=_unchanged_project_customer(ctx,item,value,remote) if target=='Customer' else None
            if link is None: raise
        if not str(link.source_id).isdigit() or int(link.source_id)<=0: raise MappedUpdateError('The related HERO identity is invalid.')
        return int(link.source_id)
    if resolver in {'DATETIME','EVENT_TIME'}:
        from hero_integration.services.native_projections import _instant
        if resolver=='EVENT_TIME':
            config=json.loads(ctx.read.mapping.mapping_json).get('native_options') or {}
            zone=ZoneInfo(config['target_timezone'])
            if ctx.document.all_day:
                day=(value if isinstance(value,datetime) else datetime.fromisoformat(str(value))).date()
                if item['source']['field']=='ends_on' and config['all_day_end']=='exclusive': day+=timedelta(days=1)
                return datetime.combine(day,time.min).replace(tzinfo=ZoneInfo(config['source_timezone'])).isoformat()
        else: zone=ZoneInfo(opts['timezone'])
        return _instant(str(value),zone).replace(microsecond=0).isoformat()
    return float(value) if isinstance(value,Decimal) else value


def _baseline_key(ctx,item):
    field=item['source']['field']
    if item['source']['doctype']=='ToDo':return 'ToDo.'+field
    if item['source']=={'doctype':'Contact','field':'phone'} and _rule(ctx,item).target_doctype=='Contact Phone':return 'hero_phone_home'
    return field


def _baseline_matches(ctx,item,remote,canonical):
    from hero_integration.services.product_migration import digest,item_values
    from hero_integration.services.change_detection import decode
    link=ctx.source_links[item['id']];state=json.loads(link.target_field_hashes or '{}');field=item['source']['field'];area=ctx.content['area']
    if item['id'] in getattr(ctx,'price_collections',{}):
        from hero_integration.services.mapped_collection_relations import _hash
        selected=ctx.price_collections[item['id']]
        return _hash({**selected['observed'],'price_list_rate':float(canonical)})==selected['target_payload_hash']
    if item['resolver']=='PRICE':
        managed=state.get('prices',{}).get(item['options']['price_list']) if area=='HeroProducts' else state.get('price')
        return isinstance(managed,dict) and float(managed.get('rate',float('nan')))==float(canonical)
    if area=='HeroProducts':
        if item['resolver']=='BARCODE':
            values=item_values(remote,SimpleNamespace(plan=ctx.query_plan));values.pop('owned',None)
            prefix='product-migration-v3:'
            version=link.mapping_version[len(prefix):] if link.mapping_version.startswith(prefix) else ''
            return bool(version) and digest({**values,'mapping':version})==link.source_payload_hash
        return state.get('fields',{}).get(field)==digest(canonical or None)
    if area=='HeroServices':
        if item['resolver']=='BARCODE':
            bars=state.get('values',{}).get('barcodes',[])
            return isinstance(bars,list) and [row.get('barcode') for row in bars if row.get('barcode')]==[canonical]
        return field in state.get('values',{}) and _json(state['values'][field])==_json(canonical)
    if area=='HeroEmployees': return field in state and str(state[field] or '')==str(canonical or '')
    if area in {'HeroDocuments','HeroPictures'}:
        from hero_integration.services.file_transfer import field_hashes,mapped_values
        return state==json.loads(field_hashes(mapped_values(SimpleNamespace(plan=ctx.query_plan),remote)))
    source_base=decode(link.source_field_hashes) or {}
    key=_baseline_key(ctx,item)
    if _native_project_notes(ctx,item):
        return _project_notes_baseline(ctx,item,remote,canonical,source_base,decode(link.target_field_hashes) or {})
    return key in source_base and key in (decode(link.target_field_hashes) or {}) and source_base[key]==send_helpers._hash(canonical)


def _remote_wire(ctx,item,doc,remote):
    canonical=_canonical(ctx,item,remote)
    if item['resolver']=='BARCODE': return canonical
    return _wire(ctx,item,doc,canonical,remote)


def _same(left,right):
    if type(left) is not bool and type(right) is not bool and isinstance(left,(int,float,Decimal)) and isinstance(right,(int,float,Decimal)):
        return Decimal(str(left))==Decimal(str(right))
    if type(left) is bool and type(right) in (int,float) or type(right) is bool and type(left) in (int,float):
        return left==right
    return _json(left)==_json(right)


def _binding_state(link):
    fields=('source_system','source_instance','source_type','source_id','target_doctype','target_name',
        'mapping_version','sync_status','source_payload_hash','observed_source_hash','target_payload_hash',
        'source_field_hashes','target_field_hashes')
    return {field:getattr(link,field,None) for field in fields}


def _prepare(ctx,client,*,preview_only=False):
    from graphql import get_named_type
    from hero_integration.services.graphql_contract import validate_mutation
    remote=_read_record(ctx,client);schema=_schema(ctx.raw_schema);operation=ctx.mapping.operation_name
    actual_id=_at(remote,ctx.content['identity']['mutation_read_path'])
    if type(actual_id) not in (int,str) or not str(actual_id).isdigit() or int(actual_id)<=0:
        raise MappedUpdateError('Approved readback did not prove the mutation record identity.')
    identity_kind=_input_type(schema,operation,ctx.content['identity']['input_path'])
    actual_id=_typed(int(actual_id) if str(get_named_type(identity_kind))=='Int' else str(actual_id),identity_kind)
    inputs={};_put(inputs,ctx.content['identity']['input_path'],actual_id)
    copied=[]
    for copy in ctx.content['required_readback']:
        if copy['input_path'].endswith('.address.id'): continue
        observed=_at(remote,copy['read_path'])
        if observed is MISSING: raise MappedUpdateError('A required unchanged input member is missing from approved readback.')
        if copy['input_path'].endswith('.id') and (type(observed) is not int or observed<=0):
            raise MappedUpdateError('The related HERO identity is invalid.')
        _put(inputs,copy['input_path'],_typed(observed,_input_type(schema,operation,copy['input_path'])))
        copied.append(copy)
    changes=[];conflicts=[];checks=[];states=[];assigned={}
    for item in ctx.content['assignments']:
        from hero_integration.services.project_naming import is_local_project_name
        if is_local_project_name(item['source'], item['input_path']):
            raise MappedUpdateError('Project naming is local to ERPNext and cannot be sent to HERO.')
        try: doc,local=_source(ctx,item,remote)
        except send_helpers._SourceRelationMissing:
            if item['options'].get('missing')=='OMIT': continue
            raise
        condition=item['options'].get('when')
        states.append({'id':item['id'],'doctype':doc.doctype,'name':doc.name,'modified':str(doc.modified),
            'value':local,'condition':doc.get(condition['field']) if condition else None,
            'binding':_binding_state(ctx.source_links[item['id']])})
        if condition and doc.get(condition['field'])!=condition['equals']: continue
        try:
            wire=_wire(ctx,item,doc,local,remote)
        except MappedUpdateBindingError as error:
            if not preview_only or ctx.content['area'] != 'HeroProjectMatches': raise
            conflicts.append({'id':item['id'],'field':item['input_path'],'source_doctype':doc.doctype,
                'source_field':item['source']['field'],
                'label':frappe.get_meta(doc.doctype).get_field(item['source']['field']).label,
                'hero':_at(remote,item['read_path'],None),'new':local,
                'blocked_reason':error.message_template})
            continue
        if wire is MISSING: continue
        canonical=_canonical(ctx,item,remote)
        kind=_input_type(schema,operation,item['input_path'])
        if str(get_named_type(kind))=='Boolean' and type(wire) is int and wire in (0,1): wire=bool(wire)
        wire=_typed(wire,kind)
        previous=_remote_wire(ctx,item,doc,remote)
        checks.append({'id':item['id'],'value':wire})
        assigned[item['input_path']]=wire
        if _same(previous,wire): continue
        change={'id':item['id'],'field':item['input_path'],'source_doctype':doc.doctype,
            'source_field':item['source']['field'],'label':frappe.get_meta(doc.doctype).get_field(item['source']['field']).label,
            'hero':None if previous is MISSING else previous,'new':wire,'local':local,'canonical_remote':canonical}
        if not _baseline_matches(ctx,item,remote,canonical): conflicts.append(change)
        else: changes.append(change)
    # All mapped event times travel together: an all-day toggle cannot silently
    # reinterpret an unchanged local end date under the other interval convention.
    selected={change['field'] for change in changes}
    if ctx.content['area']=='HeroCalendarEvents' and selected&{'calendar_event.start','calendar_event.end','calendar_event.all_day'}:
        needed={'calendar_event.start','calendar_event.end','calendar_event.all_day'}
        if not needed<=assigned.keys(): raise MappedUpdateError('Event time updates require start, end and all-day bindings together.')
        selected|=needed
    for path in selected:
        parts=path.split('.');owner=inputs
        for part in parts[:-1]: owner=owner.setdefault(part,{})
        owner[parts[-1]]=assigned[path]
    for copy in ctx.content['required_readback']:
        if not copy['input_path'].endswith('.address.id'): continue
        prefix=copy['input_path'].rsplit('.',1)[0]+'.'
        if not any(path.startswith(prefix) for path in selected): continue
        observed=_at(remote,copy['read_path'])
        if type(observed) is not int or observed<=0:
            raise MappedUpdateError('The related HERO identity is invalid.')
        _put(inputs,copy['input_path'],_typed(observed,_input_type(schema,operation,copy['input_path'])))
        copied.append(copy)
    variables={}
    for argument,value in inputs.items():
        variable=ctx.parsed.argument_variables.get(argument)
        if not variable: raise MappedUpdateError('The approved mutation service does not bind every configured input argument.')
        variables[variable]=value
    variables=validate_mutation(ctx.raw_schema,ctx.parsed,variables)
    proof=[ctx.document.doctype,ctx.document.name,str(ctx.document.modified),ctx.mapping.name,
        ctx.mapping.mapping_revision,ctx.content,ctx.contract.name,ctx.contract.contract_revision,
        ctx.contract.contract_fingerprint,ctx.service.query_document,ctx.read.contract.__dict__,
        ctx.read.service.query_document,ctx.proof.fingerprint,_binding_state(ctx.link),remote,states,variables]
    return SimpleNamespace(remote=remote,changes=changes,conflicts=conflicts,variables=variables,states=states,
        checks=checks,copied=copied,mutation_id=actual_id,fingerprint=sha256(_json(proof).encode()).hexdigest())


def preview(mapping_name,doctype,name,*,client=None):
    from hero_integration.api import _mutation_execution_state
    from hero_integration.client.graphql import HeroGraphQLClient
    from hero_integration.services.writeback_source import validate_source
    ctx=_load_context(mapping_name,doctype,name);config=validate_source(ctx.proof)
    client=client or HeroGraphQLClient(config.endpoint,lambda:config.api_token,config.timeout_seconds,max_retries=0)
    prepared=_prepare(ctx,client,preview_only=True) if ctx.content['area']=='HeroProjectMatches' else _prepare(ctx,client)
    enabled=bool(_mutation_execution_state()['mutation_execution_enabled'])
    return {'mapping':mapping_name,'doctype':doctype,'name':name,'area':ctx.content['area'],
        'hero_id':ctx.hero_id,'mutation_id':prepared.mutation_id,'create':False,'changes':prepared.changes,
        'conflicts':prepared.conflicts,'fingerprint':prepared.fingerprint,'sending_enabled':enabled,
        'can_send':enabled and bool(prepared.changes) and not prepared.conflicts}


def _check_local_current(ctx,prepared):
    items={item['id']:item for item in ctx.content['assignments']}
    for state in prepared.states:
        doc=frappe.get_doc(state['doctype'],state['name'],for_update=True)
        frappe.has_permission(doc.doctype,'write' if doc.doctype==ctx.document.doctype else 'read',doc=doc,throw=True)
        item=items[state['id']];condition=item['options'].get('when')
        if (str(doc.modified)!=state['modified'] or not _same(doc.get(item['source']['field']),state['value'])
                or condition and not _same(doc.get(condition['field']),state['condition'])):
            raise MappedUpdateError('The native document changed during Send; reconcile the remote result.')
        link=frappe.get_doc('HERO Integration Link',ctx.source_links[item['id']].name,for_update=True)
        if _binding_state(link)!=state['binding']:
            raise MappedUpdateError('The native identity or baseline changed during Send; reconcile the remote result.')


def _acknowledge(ctx,prepared,after):
    from hero_integration.services.change_detection import decode,encode
    from hero_integration.services.product_migration import digest,item_values,owned_digest
    items={item['id']:item for item in ctx.content['assignments']};updates={}
    for change in prepared.changes:
        item=items[change['id']];link=ctx.source_links[item['id']];field=item['source']['field'];area=ctx.content['area']
        data=updates.setdefault(link.name,{'source_field_hashes':decode(link.source_field_hashes) or {},
            'target_field_hashes':json.loads(link.target_field_hashes or '{}')})
        state=data['target_field_hashes'];canonical=_canonical(ctx,item,after)
        if item['id'] in getattr(ctx,'price_collections',{}):
            from hero_integration.services.mapped_collection_relations import _hash
            data['target_payload_hash']=_hash(ctx.price_collections[item['id']]['observed'])
            data['_collection']=True
            continue
        if item['resolver']=='PRICE':
            managed=state['prices'][item['options']['price_list']] if area=='HeroProducts' else state['price']
            managed['rate']=float(change['local'])
        elif area=='HeroProducts':
            if item['resolver']!='BARCODE': state.setdefault('fields',{})[field]=digest(change['local'] or None)
        elif area=='HeroServices':
            from hero_integration.services.service_migration import normalized
            value=normalized(ctx.source_documents[item['id']],{field})[field]
            state.setdefault('values',{})[field]=value
        elif area=='HeroEmployees': state[field]=str(change['local'] or '')
        elif area in {'HeroDocuments','HeroPictures'}:
            from hero_integration.services.file_transfer import field_hashes,mapped_values
            data['target_field_hashes']=json.loads(field_hashes(mapped_values(SimpleNamespace(plan=ctx.query_plan),after)))
        else:
            key=_baseline_key(ctx,item)
            data['source_field_hashes'][key]=send_helpers._hash(canonical)
            state[key]=send_helpers._hash(change['local'])
            if _native_project_notes(ctx,item):
                from hero_integration.services.direct_mapping import LEGACY_NOTES_BINDING
                data['source_field_hashes'].pop(LEGACY_NOTES_BINDING[2],None)
                state.pop(LEGACY_NOTES_BINDING[2],None)
    for name,data in updates.items():
        area=ctx.content['area'];state=data['target_field_hashes']
        if data.pop('_collection',False):
            data['source_field_hashes']=encode(data['source_field_hashes'])
            data['target_field_hashes']=_json(data['target_field_hashes'])
            frappe.db.set_value('HERO Integration Link',name,data,update_modified=False)
            continue
        if area=='HeroProducts':
            data['target_payload_hash']=owned_digest(ctx.document,tuple(state.get('fields',{})))
            values=item_values(after,SimpleNamespace(plan=ctx.query_plan));values.pop('owned',None)
            version=ctx.link.mapping_version.removeprefix('product-migration-v3:')
            data['source_payload_hash']=digest({**values,'mapping':version})
            data['observed_source_hash']=data['source_payload_hash']
        elif area=='HeroServices': data['target_payload_hash']=digest(state.get('values',{}))
        elif area=='HeroEmployees':
            from hero_integration.services.employee_migration import _digest
            data['target_payload_hash']=_digest(state)
        data['source_field_hashes']=encode(data['source_field_hashes'])
        data['target_field_hashes']=_json(data['target_field_hashes'])
        frappe.db.set_value('HERO Integration Link',name,data,update_modified=False)


def send(mapping_name,doctype,name,*,fingerprint='',confirmed=False,client=None,mutation_client=None):
    from hero_integration.api import _assert_mutation_execution_released,_reserve_mutation,_finalize_mutation
    from hero_integration.client.graphql import HeroGraphQLClient
    from hero_integration.client.graphql_mutation import HeroMutationClient
    from hero_integration.services.writeback_source import validate_source
    _assert_mutation_execution_released()
    ctx=_load_context(mapping_name,doctype,name);config=validate_source(ctx.proof)
    client=client or HeroGraphQLClient(config.endpoint,lambda:config.api_token,config.timeout_seconds,max_retries=0)
    prepared=_prepare(ctx,client)
    if confirmed not in (True,1,'1','true') or not fingerprint or fingerprint!=prepared.fingerprint:
        raise MappedUpdateError('The mapped update preview changed; review and confirm it again.')
    if prepared.conflicts or not prepared.changes:
        raise MappedUpdateError('Send requires mapped local changes without remote baseline conflicts.')
    audit=_reserve_mutation(operation_name=ctx.mapping.operation_name,schema_fingerprint=ctx.snapshot.schema_fingerprint,
        mapping_revision=int(ctx.mapping.mapping_revision),request_fingerprint=prepared.fingerprint)
    try:
        # Reservation commits its audit row. Re-prove all authority and payload
        # after that boundary, before the single allowed transport attempt.
        fresh=_load_context(mapping_name,doctype,name);verified=_prepare(fresh,client)
        if verified.fingerprint!=prepared.fingerprint:
            raise MappedUpdateError('The mapped update authority or payload changed after reservation.')
        _assert_mutation_execution_released();config=validate_source(ctx.proof)
        transport=mutation_client or HeroMutationClient(config.endpoint,lambda:config.api_token,config.timeout_seconds)
        returned=transport.execute_once(ctx.service.query_document,prepared.variables)
        record=returned.get(ctx.mapping.operation_name) if isinstance(returned,dict) else None
        if not isinstance(record,dict) or returned.get('errors') or str(record.get('id'))!=str(prepared.mutation_id):
            raise MappedUpdateError('HERO did not confirm the separately proven mutation identity.')
        validate_source(ctx.proof);after=_read_record(ctx,client)
        if str(_at(after,ctx.content['identity']['mutation_read_path']))!=str(prepared.mutation_id):
            raise MappedUpdateError('The mutation identity changed during readback.')
        for copy in prepared.copied:
            if copy['input_path'].endswith('.id') or copy['input_path'].endswith('.product_id'):
                if not _same(_at(after,copy['read_path']),_at(prepared.remote,copy['read_path'])):
                    raise MappedUpdateError('A protected related identity changed during Send.')
        items={item['id']:item for item in ctx.content['assignments']}
        for check in prepared.checks:
            item=items[check['id']]
            if not _same(_remote_wire(ctx,item,ctx.source_documents[item['id']],after),check['value']):
                raise MappedUpdateError('HERO did not retain the configured mapped field value.')
        validate_source(ctx.proof);_check_local_current(ctx,prepared);_acknowledge(ctx,prepared,after)
    except Exception:
        frappe.db.rollback()
        _finalize_mutation(audit,'Failed','Mapped update was not verified; no automatic retry. Check HERO before another submission.')
        raise MappedUpdateError('Sending to HERO could not be confirmed; no automatic retry was performed.') from None
    _finalize_mutation(audit,'Succeeded','Mapped update was sent once and verified by approved Query readback.')
    return {'status':'SENT','hero_id':ctx.hero_id,'mutation_id':prepared.mutation_id,
        'changes':[change['label'] for change in prepared.changes],'correlation_id':audit.correlation_id}
