"""Pure draft-only bridge from saved HERO introspection to graphql_protocol."""
from __future__ import annotations
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
import json, re
from hero_integration.services.graphql_graph import analyze_snapshot, normalize_schema, GraphSnapshotError
from hero_integration.graphql_protocol.schema import GraphQLSchema, GraphQLType, GraphQLField, GraphQLArgument, GraphQLEnumValue, GraphQLTypeRef
from hero_integration.graphql_protocol.schema_snapshot import schema_fingerprint
from hero_integration.graphql_protocol.operation_kind import GraphQLOperationKind
from hero_integration.graphql_protocol.graphql_operation_materializer import GraphQLOperationBinding, GraphQLOperationMaterializer, GraphQLOperationMaterializationError, GraphQLOperationRequest, GraphQLResultBinding
from hero_integration.client.graphql import HeroClientError, assert_read_only
from hero_integration.graphql_protocol.source_proposals import SourceApiProposalModel, SourceOperationProposal, SourceOperationRole
from hero_integration.services.read_contract import ReadContract
V1=1; NAME=re.compile(r'^[_A-Za-z][_0-9A-Za-z]*$'); SHA=re.compile(r'^[0-9a-f]{64}$')
class MappingContractError(ValueError): pass
class Direction(StrEnum): READ='read'; WRITE='write'
class NullMode(StrEnum): MISSING_IS_UNCHANGED='missing_is_unchanged'; EXPLICIT_NULL='explicit_null'
class RelationMode(StrEnum): SINGLE='single'; COLLECTION='collection'
def name(x):
 if not isinstance(x,str) or not NAME.fullmatch(x): raise MappingContractError('Invalid GraphQL name.')
 return x
def ref(x):
 if not isinstance(x,Mapping) or not isinstance(x.get('kind'),str): raise MappingContractError('Incomplete type reference.')
 return GraphQLTypeRef(x['kind'], of_type=ref(x.get('ofType'))) if x['kind'] in ('LIST','NON_NULL') else GraphQLTypeRef(x['kind'],name=name(x.get('name')))
def arg(x):
 if not isinstance(x,Mapping): raise MappingContractError('Malformed argument.')
 return GraphQLArgument(name(x.get('name')),x.get('description') if isinstance(x.get('description'),str) else None,ref(x.get('type')),x.get('defaultValue') if isinstance(x.get('defaultValue'),str) else None)
def schema(x):
 types=[]
 if not isinstance(x.get('types'),list): raise MappingContractError('Schema types missing.')
 for t in x['types']:
  if not isinstance(t,Mapping) or not isinstance(t.get('fields') or [],list) or not isinstance(t.get('inputFields') or [],list): raise MappingContractError('Incomplete schema type.')
  fields=tuple(GraphQLField(name(f.get('name')),None,tuple(arg(a) for a in f.get('args',[])),ref(f.get('type')),False,None) for f in t.get('fields') or [] if isinstance(f,Mapping))
  enums=tuple(GraphQLEnumValue(name(e.get('name')),None,False,None) for e in t.get('enumValues') or [] if isinstance(e,Mapping))
  types.append(GraphQLType(name(t.get('kind')),name(t.get('name')),None,fields,tuple(arg(a) for a in t.get('inputFields') or []),enums))
 roots=[]
 for key in ('queryType','mutationType','subscriptionType'):
  v=x.get(key); roots.append(None if v is None else name(v.get('name') if isinstance(v,Mapping) else None))
 if not roots[0] or any(r not in {t.name for t in types} for r in roots if r): raise MappingContractError('Schema root missing.')
 return GraphQLSchema(roots[0],roots[1],roots[2],tuple(types))
@dataclass(frozen=True,slots=True)
class StoredSnapshot: name:str; snapshot_json:Mapping[str,object]|str; fingerprint:str; source_instance:str; analysis_status:str='Validated'
@dataclass(frozen=True,slots=True)
class ValidatedSnapshot: name:str; fingerprint:str; source_instance:str; ir:GraphQLSchema; ir_fingerprint:str
class SnapshotAdapter:
 def adapt(self,s):
  if s.analysis_status!='Validated' or not SHA.fullmatch(s.fingerprint) or not s.name or not s.source_instance: raise MappingContractError('Unvalidated snapshot.')
  raw=json.loads(s.snapshot_json) if isinstance(s.snapshot_json,str) else dict(s.snapshot_json)
  try: normal=normalize_schema(raw); observed=analyze_snapshot(raw).fingerprint
  except (GraphSnapshotError,ValueError,TypeError) as e: raise MappingContractError('Malformed snapshot.') from e
  if observed!=s.fingerprint: raise MappingContractError('Stale snapshot fingerprint.')
  ir=schema(normal); return ValidatedSnapshot(s.name,s.fingerprint,s.source_instance,ir,schema_fingerprint(ir))
@dataclass(frozen=True,slots=True)
class LogicalMappingContractV1:
 definition_id:str; direction:Direction; frappe_app:str; doctype:str; fieldname:str; operation:str; source_path:tuple[str,...]; target_path:tuple[str,...]; transformation:str|None; null_mode:NullMode; external_identity_path:tuple[str,...]; relation_mode:RelationMode; snapshot_name:str; snapshot_fingerprint:str; revision:int; lifecycle:str; approval:str; version:int=V1
 def __post_init__(self):
  if self.version!=V1 or not all((self.definition_id,self.frappe_app,self.doctype,self.fieldname,self.operation,self.snapshot_name,self.lifecycle,self.approval)) or self.revision<1 or not SHA.fullmatch(self.snapshot_fingerprint): raise MappingContractError('Invalid logical mapping contract.')
  if not self.source_path or not self.target_path or not self.external_identity_path: raise MappingContractError('Identity and paths required.')
@dataclass(frozen=True,slots=True)
class QueryDraft: operation_name:str; alias:str; field:str; selected_paths:tuple[str,...]; variables:Mapping[str,object]; pagination:str|None=None; ordering:str|None=None
class QueryAdapter:
 def materialize(self,s,d):
  f=next((x for x in s.ir.query_fields if x.name==d.field),None)
  if not f: raise MappingContractError('Unknown query field.')
  args={x.name for x in f.arguments}
  if (d.pagination and d.pagination not in d.variables) or (d.ordering and d.ordering not in d.variables) or any(x and x not in args for x in (d.pagination,d.ordering)): raise MappingContractError('Invalid pagination/ordering contract.')
  b=GraphQLOperationBinding('draft:'+d.operation_name,s.ir_fingerprint,'operation:query:'+d.field,GraphQLOperationKind.QUERY,d.operation_name,d.alias,d.selected_paths)
  p=SourceApiProposalModel(1,s.ir_fingerprint,(),(),(SourceOperationProposal(b.operation_node_id,d.field,GraphQLOperationKind.QUERY,SourceOperationRole.COLLECTION,None,(),(),(),()),))
  try: return GraphQLOperationMaterializer().materialize(s.ir,p,b,d.variables)
  except GraphQLOperationMaterializationError as e: raise MappingContractError(str(e)) from e

 def materialize_personal_task(self, snapshot, content):
  """Compile selected native Task rules; this is not read authorization."""
  if not isinstance(content, Mapping) or content.get('target_operation') != 'HeroTasks':
   raise MappingContractError('An explicit native Task mapping is required.')
  scope = content.get('native_task_onboarding')
  if scope != {'confirmed': True, 'source_instance': snapshot.source_instance, 'source_type': 'Task'}:
   raise MappingContractError('Native Task onboarding scope does not match the snapshot.')
  if content.get('records_path') != 'tasks' or content.get('identity_path') != 'id':
   raise MappingContractError('Native Task requires the explicit tasks root and id identity.')
  root = next((item for item in snapshot.ir.query_fields if item.name == 'tasks'), None)
  root_type = root.type_ref if root else None
  if root_type is not None and root_type.kind == 'NON_NULL': root_type = root_type.of_type
  if root_type is None or root_type.kind != 'LIST' or root_type.named_type() != 'Task':
   raise MappingContractError('Native Task root must return the Task collection.')
  element = root_type.of_type
  if element is not None and element.kind == 'NON_NULL': element = element.of_type
  if element is None or element.kind != 'OBJECT' or element.name != 'Task':
   raise MappingContractError('Native Task root must return the Task collection.')
  if content.get('variables') != {'first': '$page_size', 'offset': '$offset'}:
   raise MappingContractError('Native Task pagination differs from the reviewed contract.')
  page_size = content.get('page_size', 100)
  if type(page_size) is not int or not 1 <= page_size <= 1000:
   raise MappingContractError('Native Task page size is invalid.')
  ids, rules = content.get('mapping_definition_ids'), content.get('field_rules')
  if (not isinstance(ids, list) or not ids or not isinstance(rules, Mapping)
      or any(not isinstance(key, str) or key not in rules or not isinstance(rules[key], Mapping) for key in ids)
      or len(set(ids)) != len(ids) or content.get('additional_mappings')):
   raise MappingContractError('Native Task field rules are incomplete or unsupported.')
  paths = [rules[key].get('source_path') for key in ids]
  if (any(not isinstance(path, str) or not path for path in paths)
      or len(paths) != len(set(paths)) or not {'id', 'title'} <= set(paths)):
   raise MappingContractError('Native Task identity and title rules are required.')
  selected = set(paths) | {'is_deleted'}
  if 'target_user_id' in paths:
   selected.add('author_user_id')
  selected = tuple(sorted(selected))
  return self.materialize(snapshot, QueryDraft('tasks', 'tasks', 'tasks', selected,
                          {'first': page_size, 'offset': 0, 'orderBy': 'id'}, pagination='offset'))
@dataclass(frozen=True,slots=True)
class ContractV1ReadAdapter:
 contract:ReadContract; request:GraphQLOperationRequest; transport_document:str; selected_paths:tuple[str,...]; result_bindings:tuple[GraphQLResultBinding,...]
def adapt_read_contract(s,contract,d,saved_query,*,offset,service_revision,mapping_revision,snapshot_fingerprint):
 if not isinstance(contract,ReadContract): raise MappingContractError('Validated read contract required.')
 if service_revision!=contract.service_revision: raise MappingContractError('Stale service revision.')
 if mapping_revision!=contract.mapping_revision: raise MappingContractError('Stale mapping revision.')
 if snapshot_fingerprint!=s.fingerprint: raise MappingContractError('Stale snapshot fingerprint.')
 if not isinstance(offset,int) or isinstance(offset,bool) or offset<0: raise MappingContractError('Invalid read offset.')
 if not isinstance(saved_query,str) or not saved_query.strip(): raise MappingContractError('Saved reviewed query required.')
 try: assert_read_only(saved_query)
 except HeroClientError as error: raise MappingContractError('Saved reviewed query must be read-only.') from error
 if d.alias!=contract.records_path or not d.selected_paths or contract.identity_path not in d.selected_paths: raise MappingContractError('Invalid result bindings.')
 variables={}
 for key,value in contract.variables.items():
  if value=='$offset': variables[key]=offset
  elif value=='$page_size': variables[key]=contract.page_size
  elif isinstance(value,str) and value.startswith('$'): raise MappingContractError('Unsupported read-contract variable.')
  else: variables[key]=value
 if dict(d.variables)!=variables: raise MappingContractError('Read-contract variable semantics differ.')
 request=QueryAdapter().materialize(s,d)
 if request.document!=saved_query: raise MappingContractError('Materialized query conflicts with saved reviewed query.')
 paths=tuple(binding.selection_path for binding in request.result_bindings)
 response_paths=tuple(binding.response_path for binding in request.result_bindings)
 expected_paths=tuple(tuple(path.split('.')) for path in d.selected_paths)
 if not request.result_bindings or paths!=expected_paths or any(path[:2]!=('data',d.alias) for path in response_paths): raise MappingContractError('Invalid result bindings.')
 return ContractV1ReadAdapter(contract,request,saved_query,tuple(d.selected_paths),request.result_bindings)
@dataclass(frozen=True,slots=True)
class MutationDraft: field:str; input_paths:tuple[tuple[str,...],...]; source_instance:str; revision:int; relation_mode:RelationMode=RelationMode.SINGLE; resolver_approved:bool=False; null_mode:NullMode=NullMode.MISSING_IS_UNCHANGED
class MutationAdapter:
 def materialize(self,s,d):
  if (not isinstance(d.source_instance,str) or d.source_instance!=s.source_instance
      or type(d.revision) is not int or d.revision<1 or not d.input_paths
      or d.null_mode is not NullMode.MISSING_IS_UNCHANGED
      or not isinstance(d.relation_mode, RelationMode)
      or (d.relation_mode is RelationMode.COLLECTION and d.resolver_approved is not True)):
   raise MappingContractError('Non-executable mutation draft is incomplete or outside the snapshot source.')
  if (not isinstance(d.input_paths,tuple) or any(not isinstance(p,tuple) or not p or
      any(not isinstance(part,str) or not NAME.fullmatch(part) for part in p) for p in d.input_paths)):
   raise MappingContractError('Mutation input paths must contain GraphQL names.')
  if any(a==b[:len(a)] for index,a in enumerate(d.input_paths) for j,b in enumerate(d.input_paths) if index!=j):
   raise MappingContractError('Mutation input paths overlap or repeat.')
  f=next((x for x in s.ir.mutation_fields if x.name==d.field),None)
  if not f: raise MappingContractError('Unknown mutation field.')
  args={a.name:a for a in f.arguments}
  input_types=[]
  def describe(input_field):
   nullable=input_field.type_ref.kind!='NON_NULL'
   return {'name':input_field.name,'type':input_field.type_ref.render(),'nullable':nullable,
           'omittable':nullable or input_field.default_value is not None,'default_value':input_field.default_value}
  for path in d.input_paths:
   current=args.get(path[0])
   if current is None: raise MappingContractError('Unknown mutation argument.')
   segments=[describe(current)]
   for segment in path[1:]:
    parent_ref=current.type_ref
    if parent_ref.kind=='NON_NULL': parent_ref=parent_ref.of_type
    if parent_ref is None or parent_ref.kind=='LIST': raise MappingContractError('Mutation collection paths require an explicit element binding.')
    parent=s.ir.type_named(parent_ref.named_type() or '')
    if parent is None or parent.kind!='INPUT_OBJECT': raise MappingContractError('Unsupported mutation input path.')
    current=next((x for x in parent.input_fields if x.name==segment),None)
    if current is None: raise MappingContractError('Unknown mutation input path.')
    segments.append(describe(current))
   leaf_ref=current.type_ref.of_type if current.type_ref.kind=='NON_NULL' else current.type_ref
   if leaf_ref is not None and leaf_ref.kind=='LIST' and d.relation_mode is not RelationMode.COLLECTION:
    raise MappingContractError('Mutation list input requires an explicitly approved collection binding.')
   input_types.append({'path':list(path),'type':current.type_ref.render(),
                       'nullable':current.type_ref.kind!='NON_NULL','segments':segments})
  return {'executable':False,'field':f.name,'snapshot_fingerprint':s.fingerprint,'source_instance':d.source_instance,
          'revision':d.revision,'input_paths':d.input_paths,'input_types':input_types,'null_mode':d.null_mode.value}

 def construct_preview_values(self,s,d,values):
  """Build schema-checked variables from explicit path-keyed sample values; never execute."""
  from hero_integration.graphql_protocol.graphql_argument_values import _validate, GraphQLArgumentValueError
  plan=self.materialize(s,d)
  if not isinstance(values,Mapping) or len(values)>100 or any(not isinstance(key,str) for key in values):
   raise MappingContractError('Mutation preview values must be a bounded path-keyed object.')
  try:
   if len(json.dumps(values,ensure_ascii=False,allow_nan=False).encode('utf-8'))>65536: raise ValueError
  except (ValueError,TypeError,RecursionError) as error:
   raise MappingContractError('Mutation preview values must be bounded JSON.') from error
  selected={'.'.join(path):path for path in d.input_paths}
  if set(values)-set(selected): raise MappingContractError('Mutation preview contains an unselected input path.')
  variables={}
  for key,path in selected.items():
   if key not in values: continue
   parent=variables
   for segment in path[:-1]: parent=parent.setdefault(segment,{})
   parent[path[-1]]=values[key]
  field=next(item for item in s.ir.mutation_fields if item.name==d.field)
  try:
   for argument in field.arguments:
    if argument.name not in variables:
     if argument.type_ref.kind=='NON_NULL' and argument.default_value is None:
      raise GraphQLArgumentValueError('A required mutation argument is missing.')
     continue
    variables[argument.name]=_validate(variables[argument.name],argument.type_ref,argument.name,schema=s.ir,strict_scalars=True)
  except (GraphQLArgumentValueError,ValueError,TypeError) as error:
   raise MappingContractError('Mutation preview variables do not satisfy the saved schema.') from error
  return {**plan,'variables':variables}
