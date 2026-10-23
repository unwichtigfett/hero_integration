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
from hero_integration.graphql_protocol.graphql_operation_materializer import GraphQLOperationBinding, GraphQLOperationMaterializer, GraphQLOperationMaterializationError
from hero_integration.graphql_protocol.source_proposals import SourceApiProposalModel, SourceOperationProposal, SourceOperationRole
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
  if (d.pagination and d.pagination not in d.variables) or any(x and x not in args for x in (d.pagination,d.ordering)): raise MappingContractError('Invalid pagination/ordering contract.')
  b=GraphQLOperationBinding('draft:'+d.operation_name,s.ir_fingerprint,'operation:query:'+d.field,GraphQLOperationKind.QUERY,d.operation_name,d.alias,d.selected_paths)
  p=SourceApiProposalModel(1,s.ir_fingerprint,(),(),(SourceOperationProposal(b.operation_node_id,d.field,GraphQLOperationKind.QUERY,SourceOperationRole.COLLECTION,None,(),(),(),()),))
  try: return GraphQLOperationMaterializer().materialize(s.ir,p,b,d.variables)
  except GraphQLOperationMaterializationError as e: raise MappingContractError(str(e)) from e
@dataclass(frozen=True,slots=True)
class MutationDraft: field:str; input_paths:tuple[tuple[str,...],...]; source_instance:str; revision:int; relation_mode:RelationMode=RelationMode.SINGLE; resolver_approved:bool=False; null_mode:NullMode=NullMode.MISSING_IS_UNCHANGED
class MutationAdapter:
 def materialize(self,s,d):
  if not d.source_instance or d.revision<1 or not d.input_paths or d.null_mode is not NullMode.MISSING_IS_UNCHANGED or (d.relation_mode is RelationMode.COLLECTION and not d.resolver_approved): raise MappingContractError('Non-executable mutation draft is incomplete.')
  f=next((x for x in s.ir.mutation_fields if x.name==d.field),None)
  if not f: raise MappingContractError('Unknown mutation field.')
  args={a.name:a for a in f.arguments}
  for path in d.input_paths:
   if not path: raise MappingContractError('Empty mutation input path.')
   current=args.get(path[0])
   if current is None: raise MappingContractError('Unknown mutation argument.')
   for segment in path[1:]:
    parent=s.ir.type_named(current.type_ref.named_type() or '')
    if parent is None or parent.kind!='INPUT_OBJECT': raise MappingContractError('Unsupported mutation input path.')
    current=next((x for x in parent.input_fields if x.name==segment),None)
    if current is None: raise MappingContractError('Unknown mutation input path.')
  return {'executable':False,'field':f.name,'snapshot_fingerprint':s.fingerprint,'source_instance':d.source_instance,'revision':d.revision,'input_paths':d.input_paths}
