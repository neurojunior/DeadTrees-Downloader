#!/usr/bin/env python3
"""DeadTrees: catálogo municipal, API oficial de COGs e downloads retomáveis.
"""
import argparse,csv,os,re,time,unicodedata
from pathlib import Path
from urllib.parse import urlparse, quote
import requests

SUPA='https://supabase.deadtrees.earth/rest/v1/v2_full_dataset_view_public'
COG=re.compile(r'https://data2\.deadtrees\.earth/cogs/v1/[0-9a-f-]{36}/(\d+)_cog\.tif',re.I)
CONTROL={6538:'https://data2.deadtrees.earth/cogs/v1/26770cd5-b19a-4b77-ba4c-40c2dccab9cf/6538_cog.tif'}

def slug(s):
 s=''.join(c for c in unicodedata.normalize('NFD',s) if unicodedata.category(c)!='Mn')
 return re.sub('[^A-Za-z0-9_-]+','_',s).strip('_')

def catalog(session,key,country,city):
 headers={'apikey':key,'Authorization':'Bearer '+key}
 last=0; out=[]
 while True:
  params={'select':'id,admin_level_1,admin_level_2,is_cog_done,cog_path,cog_file_name,cog_file_size', 'admin_level_2':f'ilike.*{city}*','id':f'gt.{last}','order':'id.asc','limit':100}
  r=session.get(SUPA,headers=headers,params=params,timeout=60);r.raise_for_status()
  batch=r.json()
  if not batch:break
  out.extend(x for x in batch if (x.get('admin_level_1') or '').casefold()==country.casefold())
  last=int(batch[-1]['id'])
  if len(batch)<100:break
 return out

def probe(session,url):
 try:
  with session.get(url,headers={'Range':'bytes=0-15'},stream=True,timeout=25) as r:
   if r.status_code!=206:return False,0
   magic=next(r.iter_content(16),b'')
   if not (magic.startswith(b'II*\x00') or magic.startswith(b'MM\x00*') or magic.startswith(b'II+\x00') or magic.startswith(b'MM\x00+')):return False,0
   cr=r.headers.get('Content-Range','')
   return True,int(cr.rsplit('/',1)[1]) if '/' in cr else 0
 except Exception:return False,0

def download(session,url,path,expected):
 """Download por blocos Range com retries e retomada persistente."""
 path.parent.mkdir(parents=True,exist_ok=True)
 if path.exists() and expected and path.stat().st_size==expected:return 'existente'
 part=path.with_suffix('.tif.part')
 if expected and part.exists() and part.stat().st_size>expected:
  part.unlink()
 chunk_size=4*1024*1024
 attempts=7
 while True:
  start=part.stat().st_size if part.exists() else 0
  if expected and start==expected:
   part.replace(path);return 'baixado'
  end=start+chunk_size-1
  if expected:end=min(end,expected-1)
  last_error=None
  for attempt in range(1,attempts+1):
   try:
    with session.get(url,headers={'Range':f'bytes={start}-{end}'},stream=True,timeout=(25,90)) as r:
     if r.status_code!=206:
      raise RuntimeError(f'Servidor nao respeitou Range (HTTP {r.status_code})')
     cr=r.headers.get('Content-Range','')
     if not cr.startswith(f'bytes {start}-'):
      raise RuntimeError(f'Content-Range inesperado: {cr}')
     # Escrever apenas depois de receber o bloco inteiro: evita fragmentos incompletos.
     block=bytearray()
     for data in r.iter_content(256*1024):
      if data:block.extend(data)
     declared=int(cr.split('-')[1].split('/')[0])-start+1
     if len(block)!=declared:raise IOError(f'Bloco incompleto: {len(block)} de {declared}')
     with part.open('ab') as f:f.write(block)
     print(f'    {part.stat().st_size/1024**2:.1f} MiB'+(f' / {expected/1024**2:.1f} MiB' if expected else ''))
     break
   except (requests.RequestException,OSError,RuntimeError) as exc:
    last_error=exc
    print(f'    tentativa {attempt}/{attempts} no byte {start}: {exc}')
    if attempt<attempts:time.sleep(min(2**attempt,20))
  else:raise RuntimeError(f'Falha apos {attempts} tentativas: {last_error}')
  if not expected:
   # Sem tamanho total conhecido, detectar ultimo bloco menor que o solicitado.
   if len(block)<chunk_size:
    part.replace(path);return 'baixado (tamanho nao confirmado)'

def resolve_official(session, base, dataset_id, token=None):
 """Consulta a rota oficial. Nao tenta contornar limite 429."""
 url=base.rstrip('/')+f'/datasets/{dataset_id}/files/cog'
 headers={'Accept':'application/json'}
 if token:headers['Authorization']='Bearer '+token
 try:
  r=session.get(url,headers=headers,timeout=(15,45))
 except requests.RequestException as e:
  return '', 'ERRO_REDE: '+str(e)[:160]
 if r.status_code==429: return '', 'LIMITE_429'
 if r.status_code==404:return '', 'NAO_PUBLICO_OU_INEXISTENTE_404'
 if r.status_code in (401,403):return '', 'ACESSO_'+str(r.status_code)
 if r.status_code!=200:return '', 'HTTP_'+str(r.status_code)
 try: relative=r.json().get('cog_path','')
 except ValueError:return '', 'RESPOSTA_NAO_JSON'
 if not isinstance(relative,str) or not relative:return '', 'CAMINHO_VAZIO'
 relative=relative.lstrip('/')
 # Caminho fornecido pelo servidor; nao supor UUID nem nome de arquivo.
 if relative.startswith('cogs/v1/'):relative=relative[len('cogs/v1/'):]
 if relative.startswith('http://') or relative.startswith('https://'):
  return relative, 'CAMINHO_OFICIAL'
 if '..' in relative.split('/'):return '', 'CAMINHO_INVALIDO'
 return 'https://data2.deadtrees.earth/cogs/v1/'+quote(relative,safe='/'), 'CAMINHO_OFICIAL'

def main():
 ap=argparse.ArgumentParser(description='DeadTrees - download municipal de COGs via API oficial')
 ap.add_argument('--pais',default='Brazil');ap.add_argument('--municipio');ap.add_argument('--uf',default='MT')
 ap.add_argument('--saida',default='downloads');ap.add_argument('--download-cog',action='store_true')
 ap.add_argument('--max-downloads',type=int,default=0)
 ap.add_argument('--api-base',default=os.environ.get('DEADTREES_API_BASE','https://data2.deadtrees.earth/api/v1'))
 ap.add_argument('--access-token',default=os.environ.get('DEADTREES_ACCESS_TOKEN',''))

 args=ap.parse_args()
 if not args.municipio and not os.environ.get('DEADTREES_NONINTERACTIVE'):
  args.pais=input(f'País [{args.pais}]: ').strip() or args.pais
  args.uf=input(f'UF (sigla) [{args.uf}]: ').strip().upper() or args.uf
 city=args.municipio or input('Município: ').strip()
 if not city:raise SystemExit('Município obrigatório.')
 if not args.municipio and not args.download_cog:
  args.download_cog=(input('Baixar os GeoTIFFs agora? [s/N]: ').strip().casefold() in ('s','sim'))
 key=os.environ.get('DEADTREES_APIKEY','').strip()
 if not key:raise SystemExit('Configure DEADTREES_APIKEY com a chave publica anon (nao use JWT pessoal).')
 s=requests.Session();s.headers.update({'User-Agent':'DeadTreesMunicipalDownloader/1.0'})
 rows=catalog(s,key,args.pais,city)
 out=Path(args.saida)/slug(args.uf.upper())/slug(city);out.mkdir(parents=True,exist_ok=True)
 print(f'[1/3] {len(rows)} datasets encontrados em {city}.')
 found={}; statuses={}
 print('[2/3] Consultando rota oficial de caminhos COG:',args.api_base)
 for i,row in enumerate(rows,1):
  did=int(row['id'])
  url,status=resolve_official(s,args.api_base,did,args.access_token)
  statuses[did]=status
  if url:found[did]=url
  print(f'  {i}/{len(rows)} ID {did}: {status}')
  if status=='LIMITE_429':
   print('Limite de acesso atingido. Interrompendo consultas sem novas tentativas.')
   break
  if status.startswith('ACESSO_') or status in ('HTTP_404','HTTP_502','HTTP_503'):
   print('Verifique --api-base e autorizacao. Interrompendo para evitar chamadas desnecessarias.')
   break
 print('[2/3] Validando URLs capturadas...')
 records=[]
 for row in rows:
  did=int(row['id']);url=found.get(did,'');ok,size=probe(s,url) if url else (False,0)
  records.append({'id':did,'municipio':city,'url':url if ok else '', 'status':'VALIDADO' if ok else statuses.get(did,'NAO_CONSULTADO') if not url else 'URL_NAO_VALIDADA','bytes':size})
  print(f'  ID {did}: '+('VALIDADO' if ok else 'SEM URL VALIDADA'))
 with (out/'catalogo_cogs.csv').open('w',newline='',encoding='utf-8-sig') as f:
  w=csv.DictWriter(f,fieldnames=records[0].keys() if records else ['id']);w.writeheader();w.writerows(records)
 valid=[x for x in records if x['status']=='VALIDADO']
 print(f'[3/3] {len(valid)}/{len(rows)} URLs validadas. CSV: {out/"catalogo_cogs.csv"}')
 if args.download_cog:
  for i,r in enumerate(valid[:args.max_downloads] if args.max_downloads else valid,1):
   try:
    result=download(s,r['url'],out/f"{r['id']}_cog.tif",r['bytes'])
    print(f'  Download {i}: ID {r["id"]} {result}')
   except Exception as e:print(f'  Download {i}: ID {r["id"]} ERRO: {e}')
 if len(valid)<len(rows):print('ATENCAO: IDs sem URL nao significam COG inexistente. Confira status da API.')
if __name__=='__main__':main()
