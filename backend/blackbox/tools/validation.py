"""Deterministic argument repair and actionable errors; never grants permission."""
import errno
import re

import httpx
from pydantic import ValidationError

from .base import ToolError
from ..core.scope import ScopeDenied


EXAMPLES={
    'host.preflight':{},'host.diagnostics':{'sections':['system','memory']},
    'workspace.list':{'path':'.'},'workspace.read':{'path':'notes.txt','max_bytes':8192},
    'workspace.search':{'path':'.','pattern':'flag','cap':30},
    'net.ping':{'target':'127.0.0.1'},
    'net.scan_ports':{'target':'127.0.0.1','ports':'80,8080','service_detection':True},
    'web.inspect':{'url':'http://127.0.0.1:8080/','method':'GET'},
    'web.discover_paths':{'url':'http://127.0.0.1:8080/','rate':2,'concurrency':1,'timeout_seconds':40},
    'web.technologies':{'url':'http://127.0.0.1:8080/'},'web.tls':{'url':'https://127.0.0.1:8443/'},
    'scan.lookup':{},'scan.parse':{'path':'scan.xml','format':'nmap_xml'},
    'ssh.probe':{'target':'127.0.0.1','profile':'configured-profile'},
    'ssh.list_dir':{'target':'127.0.0.1','profile':'configured-profile','path':'/home'},
    'ssh.find_files':{'target':'127.0.0.1','profile':'configured-profile','path':'/home','patterns':['flag.txt']},
    'ssh.read_file':{'target':'127.0.0.1','profile':'configured-profile','path':'/home/lab/flag.txt'},
    'artifacts.save':{'filename':'report.md','text':'Evidence-backed report'},
}


def normalize(spec,raw,settings):
    if not isinstance(raw,dict):
        raise ValueError('Tool arguments must be one JSON object using the registered parameter names')
    args=dict(raw)
    if spec.name.startswith('workspace.') and isinstance(args.get('path'),str):
        from .workspace import normalize_path,local_path
        root=settings.data_dir/'workspace'
        if root.is_symlink():
            raise ScopeDenied('Configured workspace root must not be a symlink')
        try:
            args['path']=normalize_path(root,args['path'])
            local_path(root,args['path'])
        except ValueError as exc:
            if 'Credential and secret' in str(exc) or 'escapes workspace' in str(exc):
                raise ScopeDenied(str(exc)) from exc
            raise
    if spec.name.startswith('web.') and isinstance(args.get('method'),str) and args['method'].upper() in {'GET','HEAD'}:
        args['method']=args['method'].upper()
    for name,field in spec.argument_schema.model_fields.items():
        value=args.get(name)
        if field.annotation is int and isinstance(value,str) and re.fullmatch(r'[0-9]{1,10}',value):
            args[name]=int(value)
        elif field.annotation is bool and value in ('true','false'):
            args[name]=value=='true'
    return args


def classify_error(exc,tool):
    fields=[]
    if isinstance(exc,ScopeDenied):
        kind,code='policy_denial','POLICY_DENIED'
    elif isinstance(exc,ValidationError):
        kind,code='validation','INVALID_ARGUMENTS'
        fields=[{'field':'.'.join(map(str,e['loc'])),'message':e['msg'],'type':e['type']} for e in exc.errors(include_input=False,include_context=False,include_url=False)][:16]
    elif isinstance(exc,(FileNotFoundError,IsADirectoryError,NotADirectoryError)):
        kind,code='permanent','WORKSPACE_FILE_UNAVAILABLE' if tool.startswith('workspace.') else 'RESOURCE_NOT_FOUND'
    elif isinstance(exc,PermissionError):
        kind,code='permanent','ACCESS_UNAVAILABLE'
    elif isinstance(exc,(TimeoutError,httpx.TimeoutException,httpx.NetworkError)) or isinstance(exc,OSError) and exc.errno in {errno.ECONNREFUSED,errno.ECONNRESET,errno.ETIMEDOUT,errno.ENETUNREACH,errno.EHOSTUNREACH}:
        kind,code='transient','EXECUTION_TRANSIENT'
    elif str(exc).lower().startswith(('needs access','missing binary')):
        kind,code='unavailable','ACCESS_OR_TOOL_UNAVAILABLE'
    elif isinstance(exc,(ValueError,TypeError)):
        kind,code='validation','INVALID_ARGUMENTS'
    else:
        kind,code='permanent','EXECUTION_FAILED'
    if tool.startswith('workspace.'):
        suggestions=['Call workspace.list with path="." or workspace.search, then use an exact returned relative file path.',
                     'workspace.read accesses only the local application workspace; remote /home files require separately authorized configured SFTP access. Never guess or strip an absolute path to its basename.']
    elif tool.startswith('web.'):
        suggestions=['Use an HTTP(S) URL with an authorized scheme, host and port from current evidence; no credentials or fragments.',
                     'Inspect recorded service/scan evidence before choosing a different endpoint. Do not repeat this identical failed request.']
    else:
        suggestions=['Use the registered tool schema and permitted scope/limits. Correct the named fields or choose another permitted action; never repeat an identical deterministic failure.']
    return ToolError(kind=kind,code=code,message=str(exc)[:1000] or type(exc).__name__,retryable=kind=='transient',fields=fields,suggestions=suggestions)
