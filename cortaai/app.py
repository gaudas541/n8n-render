"""CortaAI Studio — local, single-user video clipping service.
Run with: uvicorn app:app --host 127.0.0.1 --port 8000
Never expose to the public internet without authentication, TLS and quotas.
"""
from __future__ import annotations
import asyncio, json, os, re, uuid, ipaddress, socket, subprocess, shutil, time, base64, secrets, threading, zipfile
from pathlib import Path
from urllib.parse import urlparse, unquote
from typing import Any
import httpx
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from video_links import validate_video_url, download_source

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get('CORTAAI_DATA', ROOT / 'data'))
VIDS, EXPORTS, TEMPS = (DATA / x for x in ('videos','exports','tmp'))
for d in (VIDS, EXPORTS, TEMPS): d.mkdir(parents=True, exist_ok=True)
# Basic authentication protects all files and API routes when deployed.
# Public deployments MUST define CORTAAI_REQUIRE_AUTH=1 and CORTAAI_ACCESS_PASSWORD.
REQUIRE_AUTH = os.environ.get('CORTAAI_REQUIRE_AUTH', '0') == '1'
ACCESS_PASSWORD = os.environ.get('CORTAAI_ACCESS_PASSWORD', '')
if REQUIRE_AUTH and len(ACCESS_PASSWORD) < 16:
 raise RuntimeError('Public deployment requires CORTAAI_ACCESS_PASSWORD (16+ characters).')
app = FastAPI(title='CortaAI Studio', version='1.1.0')

@app.middleware('http')
async def access_control(request: Request, call_next):
 if REQUIRE_AUTH and request.url.path != '/api/health':
  raw = request.headers.get('authorization','')
  try:
   scheme, data = raw.split(' ', 1)
   decoded = base64.b64decode(data, validate=True).decode('utf-8') if scheme.lower() == 'basic' else ''
   username, passwd = decoded.split(':', 1)
  except (ValueError, UnicodeDecodeError, base64.binascii.Error):
   username, passwd = '', ''
  if not (secrets.compare_digest(username,'studio') and secrets.compare_digest(passwd, ACCESS_PASSWORD)):
   return JSONResponse({'detail':'Autenticação necessária'},status_code=401,headers={
    'WWW-Authenticate':'Basic realm="CortaAI Studio", charset="UTF-8"',
    'Cache-Control':'no-store', 'X-Robots-Tag':'noindex, nofollow',
   })
 response = await call_next(request)
 response.headers['X-Content-Type-Options'] = 'nosniff'
 response.headers['X-Frame-Options'] = 'DENY'
 response.headers['Referrer-Policy'] = 'same-origin'
 response.headers['X-Robots-Tag'] = 'noindex, nofollow'
 response.headers['Cache-Control'] = 'private, no-store'
 return response
app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')


# Password gate for public deployment. When configured, all app routes require
# HTTP Basic Auth so uploaded videos and API access are not public.
import base64
import hmac
from fastapi.responses import Response

@app.middleware("http")
async def private_studio_access(request: Request, call_next):
    password = os.environ.get("CORTAAI_PASSWORD", "")
    if password and request.url.path != "/api/health":
        credentials = request.headers.get("authorization", "")
        authenticated = False
        if credentials.startswith("Basic "):
            try:
                raw = base64.b64decode(credentials[6:], validate=True).decode("utf-8")
                username, sep, supplied = raw.partition(":")
                authenticated = bool(sep) and hmac.compare_digest(username, "studio") and hmac.compare_digest(supplied, password)
            except (ValueError, UnicodeDecodeError):
                pass
        if not authenticated:
            return Response("Autenticação necessária", status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="CortaAI Studio"', "Cache-Control": "no-store"})
    return await call_next(request)

JOBS: dict[str, dict] = {}
MAX_VIDEO = int(os.environ.get('CORTAAI_MAX_VIDEO_BYTES', 2 * 1024**3))
LINK_JOBS: dict[str, dict] = {}
LINK_LOCK = threading.Lock()
LINK_BUSY = False

DEFAULTS = {
 'nvidia': 'https://integrate.api.nvidia.com/v1',
 'groq': 'https://api.groq.com/openai/v1',
 'openai': 'https://api.openai.com/v1',
}

@app.get('/', response_class=HTMLResponse)
async def index(): return (ROOT/'static/index.html').read_text('utf-8')

@app.get('/api/health')
def health():
 return {'ok': True, 'ffmpeg': shutil.which('ffmpeg') is not None, 'ffprobe': shutil.which('ffprobe') is not None}


def clean_id(value: str) -> str:
 try: return str(uuid.UUID(value))
 except (ValueError, TypeError): raise HTTPException(400, 'ID inválido')

def video_paths(uid: str):
 uid=clean_id(uid)
 return VIDS/f'{uid}.bin', VIDS/f'{uid}.json'

def get_video(uid: str) -> dict:
 f,m=video_paths(uid)
 if not f.exists() or not m.exists(): raise HTTPException(404, 'Vídeo não encontrado')
 return json.loads(m.read_text('utf-8'))

def probe_video(path: Path) -> dict:
 try:
  p=subprocess.run(['ffprobe','-v','error','-show_entries','format=duration,size:stream=codec_type,width,height','-of','json',str(path)],capture_output=True,text=True,timeout=30,check=True)
  j=json.loads(p.stdout); st=next((s for s in j['streams'] if s['codec_type']=='video'),None)
  if not st: raise ValueError('Arquivo não contém vídeo válido')
  dur=float(j['format'].get('duration',0)); w=int(st.get('width',0)); h=int(st.get('height',0))
  if not 0 < dur <= 24*3600 or not w or not h: raise ValueError('Metadados inválidos')
  return {'duration': dur, 'width': w, 'height': h, 'size': path.stat().st_size, 'hasAudio': any(s['codec_type']=='audio' for s in j['streams'])}
 except (subprocess.SubprocessError, ValueError, KeyError, json.JSONDecodeError) as e: raise HTTPException(400, f'Não foi possível ler o vídeo: {e}')

@app.put('/api/videos/{uid}')
async def upload(uid: str, request: Request):
 f,m=video_paths(uid)
 name=unquote(request.headers.get('x-filename','video.mp4'))[:140]
 if f.exists(): raise HTTPException(409,'Já existe um vídeo com esse ID')
 total=0
 try:
  with f.open('wb') as w:
   async for block in request.stream():
    total += len(block)
    if total>MAX_VIDEO: raise HTTPException(413,'Vídeo muito grande para este servidor')
    w.write(block)
  if total<1024: raise HTTPException(400,'Vídeo vazio ou inválido')
  meta=probe_video(f)
  meta.update({'id':uid,'name':name,'createdAt':int(time.time()*1000)})
  m.write_text(json.dumps(meta,ensure_ascii=False),'utf-8')
  return meta
 except BaseException:
  f.unlink(missing_ok=True)
  raise

class ImportLinkRequest(BaseModel):
 url: str = Field(min_length=1, max_length=500)
 rightsConfirmed: bool = False


def import_link_worker(job_id: str, url: str, platform: str, video_id: str):
 global LINK_BUSY
 job = LINK_JOBS[job_id]
 f, m = video_paths(video_id)
 try:
  # This worker is intentionally single-file and single-job; Render Free has
  # limited CPU/memory/disk and in-memory jobs are lost on restart.
  result = download_source(url, job, TEMPS / (video_id + '.download'), max_bytes=MAX_VIDEO)
  # download_source writes to a temporary .download target (not a video ext);
  # ffprobe reads by probing file contents rather than the suffix.
  temp = TEMPS / (video_id + '.download')
  temp.replace(f)
  meta = probe_video(f)
  name = result['title'] + result['ext']
  meta.update({'id': video_id, 'name': name, 'createdAt': int(time.time()*1000),
               'origin': 'link', 'platform': platform, 'sourceUrl': url,
               'uploader': result['uploader']})
  sub_path = result.get('subtitle_path')
  if sub_path and Path(sub_path).is_file():
   output = VIDS / (video_id + Path(sub_path).suffix.lower())
   Path(sub_path).replace(output)
   meta['hasSubtitles'] = True
  else:
   meta['hasSubtitles'] = False
  m.write_text(json.dumps(meta, ensure_ascii=False), 'utf-8')
  job.update(status='done', progress=1, message='Vídeo importado', video=meta)
 except Exception as ex:
  f.unlink(missing_ok=True)
  m.unlink(missing_ok=True)
  # Avoid exposing private extractor URLs or internal paths in error messages.
  message = str(ex)
  if len(message) > 300 or 'https://' in message or '/tmp/' in message:
   message = 'Falha ao importar: vídeo indisponível, acima do limite ou plataforma bloqueou o download.'
  job.update(status='error', progress=0, message=message)
 finally:
  for item in TEMPS.glob(video_id + '.*'):
   if item.is_file(): item.unlink(missing_ok=True)
  with LINK_LOCK:
   LINK_BUSY = False


@app.post('/api/import-link')
async def import_by_link(r: ImportLinkRequest):
 global LINK_BUSY
 if not r.rightsConfirmed:
  raise HTTPException(400, 'Confirme que o vídeo é seu ou que possui autorização para editá-lo.')
 url, platform = validate_video_url(r.url)
 if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
  raise HTTPException(503, 'FFmpeg indisponível no servidor.')
 with LINK_LOCK:
  if LINK_BUSY: raise HTTPException(429, 'Já há um link sendo importado. Conclua a importação atual antes de iniciar outra.')
  LINK_BUSY = True
 video_id = str(uuid.uuid4())
 job_id = str(uuid.uuid4())
 LINK_JOBS[job_id] = {'status': 'queued', 'progress': 0, 'message': 'Preparando importação', 'platform': platform}
 if len(LINK_JOBS) > 30:
  for old in list(LINK_JOBS)[:-30]: LINK_JOBS.pop(old, None)
 asyncio.create_task(asyncio.to_thread(import_link_worker, job_id, url, platform, video_id))
 return {'jobId': job_id, 'platform': platform}


@app.get('/api/import-link/{job_id}')
def get_import_status(job_id: str):
 job_id = clean_id(job_id)
 result = LINK_JOBS.get(job_id)
 if result is None: raise HTTPException(404, 'Importação não encontrada; o servidor pode ter reiniciado.')
 return result


@app.get('/api/videos/{uid}/subtitles')
def imported_subtitles(uid: str):
 get_video(uid)
 for ext in ('.vtt', '.srt'):
  path = VIDS / (clean_id(uid) + ext)
  if path.is_file():
   return {'content': path.read_text('utf-8', errors='replace')[:2_000_000], 'format': ext[1:]}
 raise HTTPException(404, 'Nenhuma legenda disponível no vídeo importado.')


@app.get('/api/videos')
def list_videos():
 out=[]
 for p in VIDS.glob('*.json'):
  try: out.append(json.loads(p.read_text('utf-8')))
  except Exception: pass
 return sorted(out,key=lambda x:-x['createdAt'])

@app.get('/api/videos/{uid}')
def detail(uid:str): return get_video(uid)

@app.delete('/api/videos/{uid}')
def delete_video(uid:str):
 f,m=video_paths(uid)
 f.unlink(missing_ok=True); m.unlink(missing_ok=True)
 for ext in ('.jpg','.png','.srt','.vtt'): (VIDS/f'{clean_id(uid)}{ext}').unlink(missing_ok=True)
 return {'ok':True}

@app.get('/media/{uid}')
def media(uid: str):
 meta=get_video(uid); f,_=video_paths(uid)
 name=meta['name'].lower()
 mime='video/mp4' if name.endswith(('.mp4','.m4v','.mov')) else ('video/webm' if name.endswith('.webm') else 'application/octet-stream')
 return FileResponse(f,media_type=mime,headers={'Cache-Control':'private, max-age=3600','Accept-Ranges':'bytes'})

@app.get('/api/videos/{uid}/thumbnail')
def thumbnail(uid:str):
 meta=get_video(uid); f,_=video_paths(uid); out=VIDS/f'{clean_id(uid)}.jpg'
 if not out.exists():
  subprocess.run(['ffmpeg','-v','error','-y','-ss',str(min(1.0,meta['duration']*.1)),'-i',str(f),'-frames:v','1','-vf','scale=320:-2','-q:v','5',str(out)],capture_output=True,timeout=60)
 if not out.exists(): raise HTTPException(404,'Miniatura indisponível')
 return FileResponse(out,media_type='image/jpeg')

@app.get('/api/videos/{uid}/waveform')
def waveform(uid:str):
 meta=get_video(uid)
 if not meta['hasAudio']: raise HTTPException(404,'Sem trilha de áudio')
 f,_=video_paths(uid); out=VIDS/f'{clean_id(uid)}.png'
 if not out.exists():
  subprocess.run(['ffmpeg','-v','error','-y','-i',str(f),'-filter_complex','aformat=channel_layouts=mono,showwavespic=s=1200x130:colors=0x9BE88F','-frames:v','1',str(out)],capture_output=True,timeout=120)
 if not out.exists(): raise HTTPException(404,'Waveform indisponível')
 return FileResponse(out,media_type='image/png')

class Cue(BaseModel):
 start: float
 end: float
 text: str = Field(max_length=600)

class ExportIn(BaseModel):
 videoId: str
 start: float
 end: float
 aspect: str = '9:16'
 width: int = 720
 height: int = 1280
 fit: str = 'crop'
 offsetX: float = 0
 offsetY: float = 0
 zoom: float = 1
 volume: float = 1
 fadeIn: float = 0
 fadeOut: float = 0
 captions: bool = True
 captionStyle: str = 'bold-yellow'
 captionSize: float = 0.052
 captionY: float = 0.76
 uppercase: bool = False
 cues: list[Cue] = Field(default_factory=list, max_length=1000)


def ass_time(t:float) -> str:
 t=max(0, t); return f'{int(t//3600)}:{int(t//60)%60:02d}:{int(t)%60:02d}.{int(t%1*100):02d}'

def escape_ass(text:str) -> str:
 return text.replace('\\','\\\\').replace('{','\\{').replace('}','\\}').replace('\n','\\N').replace('\r','')

def write_ass(path: Path, e: ExportIn):
 H=e.height; W=e.width
 font=max(20,int(H*max(.02,min(.12,e.captionSize))))
 colors={'bold-yellow':'&H0000E8FF','minimal-white':'&H00FFFFFF','big-shorts':'&H00FFFFFF'}
 col=colors.get(e.captionStyle,colors['bold-yellow'])
 outline=0 if e.captionStyle=='minimal-white' else 4
 bold=-1 if e.captionStyle!='minimal-white' else 0
 txt=f'''[Script Info]\nScriptType: v4.00+\nPlayResX: {W}\nPlayResY: {H}\nScaledBorderAndShadow: yes\n\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\nStyle: Default,DejaVu Sans,{font},{col},&H000000FF,&H00000000,&H60000000,{bold},0,0,0,100,100,0,0,1,{outline},1,5,28,28,30,1\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n'''
 start,end=e.start,e.end
 x=int(W/2); y=int(H*max(.1,min(.9,e.captionY)))
 for c in e.cues:
  a=max(0,c.start-start); b=min(end,c.end)-start
  if b<=a or not c.text.strip(): continue
  txt+=f'Dialogue: 0,{ass_time(a)},{ass_time(b)},Default,,0,0,0,,{{\\pos({x},{y})}}{escape_ass(c.text.upper() if e.uppercase else c.text)}\n'
 path.write_text(txt,'utf-8')

def encode_clip(e:ExportIn, job:str):
 meta=get_video(e.videoId); f,_=video_paths(e.videoId)
 if e.start<0 or e.end<=e.start+.25 or e.end>meta['duration']+.1: raise ValueError('Intervalo de corte inválido')
 if e.width>1920 or e.height>1920 or e.width<240 or e.height<240 or e.width%2 or e.height%2: raise ValueError('Resolução inválida')
 if e.end-e.start>900: raise ValueError('O limite por clipe é 15 minutos')
 if e.fit not in ('crop','letterbox') or e.aspect not in ('9:16','1:1','16:9'): raise ValueError('Formato inválido')
 W,H=e.width,e.height; sw,sh=meta['width'],meta['height']; zoom=max(1,min(3,e.zoom)); ox=max(-1,min(1,e.offsetX)); oy=max(-1,min(1,e.offsetY))
 if e.fit=='crop':
  r=W/H
  cw=min(sw,sh*r); ch=cw/r; cw/=zoom; ch/=zoom
  x=(sw-cw)/2*(1+ox); y=(sh-ch)/2*(1+oy)
  vf=f'crop={max(2,int(cw)//2*2)}:{max(2,int(ch)//2*2)}:{int(x)//2*2}:{int(y)//2*2},scale={W}:{H}:flags=lanczos'
 else:
  r=sw/sh; cw=sw/zoom; ch=sh/zoom; x=(sw-cw)/2*(1+ox); y=(sh-ch)/2*(1+oy)
  vf=f'crop={max(2,int(cw)//2*2)}:{max(2,int(ch)//2*2)}:{int(x)//2*2}:{int(y)//2*2},scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black'
 vf+=',setsar=1'
 ass=TEMPS/f'{job}.ass'; out=EXPORTS/f'{job}.mp4'
 if e.captions and e.cues:
  write_ass(ass,e); vf+=f',ass={ass.name}'
 vf+=',format=yuv420p'
 duration=e.end-e.start
 args=['ffmpeg','-threads','2','-y','-hide_banner','-loglevel','error','-progress','pipe:1','-nostats','-ss',str(e.start),'-i',str(f),'-t',str(duration),'-map','0:v:0','-map','0:a?','-vf',vf,'-c:v','libx264','-preset','veryfast','-crf','24','-pix_fmt','yuv420p']
 if meta['hasAudio']:
  af=[f'volume={max(0,min(2,e.volume))}']
  if e.fadeIn>0: af.append(f'afade=t=in:st=0:d={min(e.fadeIn,duration)}')
  if e.fadeOut>0: af.append(f'afade=t=out:st={max(0,duration-e.fadeOut)}:d={min(e.fadeOut,duration)}')
  args+=['-af',','.join(af),'-c:a','aac','-b:a','128k']
 args+=['-movflags','+faststart',str(out)]
 JOBS[job]={'status':'processing','progress':0,'output':None}
 try:
  with subprocess.Popen(args,cwd=str(TEMPS),stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,bufsize=1) as p:
   for line in p.stdout or []:
    if line.startswith('out_time='):
     raw=line.split('=',1)[1].strip()
     try:
      hh,mm,ss=raw.split(':'); val=int(hh)*3600+int(mm)*60+float(ss)
      JOBS[job]['progress']=min(.99,max(0,val/duration))
     except ValueError: pass
   err=p.stderr.read()[-3000:] if p.stderr else ''
   if p.wait()!=0: raise RuntimeError('Falha na codificação: '+err)
  JOBS[job]={'status':'done','progress':1,'output':str(out)}
 except Exception as ex:
  out.unlink(missing_ok=True); JOBS[job]={'status':'error','progress':0,'error':str(ex)[:700]}
 finally: ass.unlink(missing_ok=True)

@app.post('/api/exports')
async def start_export(e:ExportIn):
 meta=get_video(e.videoId)
 if not (0<=e.start<e.end<=meta['duration']+.1): raise HTTPException(400,'Corte inválido')
 job=str(uuid.uuid4()); JOBS[job]={'status':'queued','progress':0}
 asyncio.create_task(asyncio.to_thread(encode_clip,e,job))
 return {'jobId':job}

class BatchExportIn(BaseModel):
 clips: list[ExportIn] = Field(min_length=1, max_length=5)


def render_batch_exports(clips: list[ExportIn], batch_id: str):
 path = EXPORTS / f'{batch_id}.zip'
 children=[]
 try:
  with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_STORED) as archive:
   for i,clip in enumerate(clips):
    subjob=str(uuid.uuid4())
    JOBS[batch_id]={'status':'processing','progress':i/len(clips),'message':f'Gerando clipe {i+1} de {len(clips)}…'}
    encode_clip(clip, subjob)
    item=JOBS[subjob]
    if item['status']!='done':
     raise RuntimeError(item.get('error', 'Falha ao exportar um dos clipes.'))
    file=Path(item['output'])
    archive.write(file, arcname=f'CortaAI-{i+1:02d}.mp4')
    children.append(file)
    JOBS.pop(subjob,None)
   JOBS[batch_id]={'status':'done','progress':1,'output':str(path),'message':f'{len(clips)} MP4 prontos no ZIP'}
 except Exception as ex:
  path.unlink(missing_ok=True)
  JOBS[batch_id]={'status':'error','progress':0,'error':str(ex)[:350]}
 finally:
  for file in children:
   file.unlink(missing_ok=True)


@app.post('/api/exports/batch')
async def start_batch_export(r: BatchExportIn):
 if len(r.clips)>5:
  raise HTTPException(400,'Este servidor permite até 5 vídeos por lote.')
 for clip in r.clips:
  meta=get_video(clip.videoId)
  if not (0<=clip.start<clip.end<=meta['duration']+.05):
   raise HTTPException(400,'Intervalo inválido em um dos cortes.')
  if clip.end-clip.start>90:
   raise HTTPException(400,'Cada corte em lote deve ter no máximo 90 segundos.')
 job=str(uuid.uuid4())
 JOBS[job]={'status':'queued','progress':0}
 asyncio.create_task(asyncio.to_thread(render_batch_exports,r.clips,job))
 return {'jobId':job}


@app.get('/api/exports/{job}')
def export_status(job:str):
 job=clean_id(job)
 if job not in JOBS: raise HTTPException(404,'Exportação não encontrada')
 j=JOBS[job].copy(); j.pop('output',None)
 if j['status']=='done': j['downloadUrl']=f'/api/exports/{job}/download'
 return j

@app.get('/api/exports/{job}/download')
def download(job:str):
 job=clean_id(job); j=JOBS.get(job,{})
 if j.get('status')!='done': raise HTTPException(404,'Exportação ainda não concluída')
 return FileResponse(j['output'],media_type='application/zip' if j['output'].endswith('.zip') else 'video/mp4',filename=f'CortaAI-{job[:8]}.zip' if j['output'].endswith('.zip') else f'CortaAI-{job[:8]}.mp4')


def safe_base(provider:str, base:str, allowed_custom: bool=True) -> str:
 if provider in DEFAULTS: return DEFAULTS[provider]
 if provider not in ('custom','riva'): raise HTTPException(400,'Provedor inválido')
 if os.environ.get('CORTAAI_DISABLE_CUSTOM_API') == '1': raise HTTPException(403,'Endpoints personalizados desativados neste servidor público')
 url=base.strip().rstrip('/'); p=urlparse(url)
 if p.scheme!='https' or not p.hostname or p.username or p.password or p.port not in (None,443): raise HTTPException(400,'Endereço deve ser HTTPS público')
 if len(url)>240 or p.query or p.fragment or '@' in url: raise HTTPException(400,'Endereço de API inválido')
 host=p.hostname.lower()
 if host=='localhost' or host.endswith(('.local','.internal','.localhost','.test')): raise HTTPException(400,'Endereço privado não permitido')
 try:
  ips=socket.getaddrinfo(host,443,proto=socket.IPPROTO_TCP)
  if not ips: raise ValueError('DNS vazio')
  for a in ips:
   ip=ipaddress.ip_address(a[4][0]);
   if not ip.is_global: raise HTTPException(400,'Host resolve para rede não pública')
 except (socket.gaierror,ValueError) as ex: raise HTTPException(400,f'Host não resolvido: {ex}')
 return url

class LlmReq(BaseModel):
 provider: str='nvidia'
 baseURL: str=''
 model: str='meta/llama-3.1-8b-instruct'
 key: str=Field(min_length=4,max_length=1000)
 transcript: list[Cue]=Field(default_factory=list,max_length=1200)
 count:int=5
 duration:float=0
 minSeconds:float=15
 maxSeconds:float=60

async def chat_call(r:LlmReq, messages:list[dict], output_tokens:int=1500):
 base=safe_base(r.provider,r.baseURL)
 endpoint=base.rstrip('/')+'/chat/completions'
 if len(r.model)>180 or not r.model or any(c.isspace() for c in r.model): raise HTTPException(400,'Nome do modelo inválido')
 async with httpx.AsyncClient(timeout=70,follow_redirects=False) as client:
  try: resp=await client.post(endpoint,headers={'Authorization':f'Bearer {r.key}','Content-Type':'application/json'},json={'model':r.model,'messages':messages,'temperature':0.3,'max_tokens':output_tokens})
  except httpx.RequestError as ex: raise HTTPException(502,f'Erro de conexão com a API: {str(ex)[:180]}')
 if resp.status_code>=400: raise HTTPException(resp.status_code, f'API retornou {resp.status_code}: {resp.text[:350]}')
 try: return resp.json()['choices'][0]['message']['content']
 except (KeyError,IndexError,ValueError) as ex: raise HTTPException(502,'Resposta incompatível da API')

@app.post('/api/test-llm')
async def test_llm(r:LlmReq):
 content=await chat_call(r,[{'role':'user','content':'Responda apenas OK.'}],20)
 return {'ok': True,'answer':str(content)[:150]}

@app.post('/api/analyze')
async def analyze(r:LlmReq):
 if not r.transcript: raise HTTPException(400,'Adicione uma transcrição com tempos antes da IA')
 if len(r.transcript)>1200: raise HTTPException(413,'Transcrição longa demais')
 span=min(20000,len(json.dumps([c.model_dump() for c in r.transcript],ensure_ascii=False)))
 transcript=json.dumps([{'start':round(c.start,2),'end':round(c.end,2),'text':c.text} for c in r.transcript],ensure_ascii=False)[:span]
 system=('Você é um editor de Shorts. Identifique trechos contíguos IMPACTANTES para cortes virais baseados SOMENTE na transcrição com tempos. '
         'Responda SOMENTE um objeto JSON no formato {"clips":[{"start":12.3,"end":45.2,"title":"...","hook":"...","reason":"...","score":80}]} sem markdown. '
         'Não invente tempos, considere frases completas, duração mínima e máxima. Score é avaliação subjetiva, não garantia de viralizar.')
 prompt=f'Gere até {max(1,min(10,r.count))} clipes entre {max(3,r.minSeconds)} e {min(180,r.maxSeconds)} segundos, vídeo total {r.duration}s. Transcrição: {transcript}'
 raw=await chat_call(r,[{'role':'system','content':system},{'role':'user','content':prompt}],2300)
 try:
  txt=re.sub(r'^```(?:json)?\s*|\s*```$','',str(raw).strip(),flags=re.I)
  a=txt.index('{'); b=txt.rindex('}')+1; parsed=json.loads(txt[a:b])
 except Exception: raise HTTPException(502,'A IA não retornou JSON de clipes válido')
 out=[]
 for c in (parsed.get('clips',[]) if isinstance(parsed,dict) else []):
  try:
   s=float(c['start']); e=float(c['end'])
   if not (0<=s<e<=r.duration+.1 and r.minSeconds-2<=e-s<=r.maxSeconds+2): continue
   out.append({'start':s,'end':e,'title':str(c.get('title','Corte sugerido'))[:90], 'hook':str(c.get('hook',''))[:200], 'reason':str(c.get('reason',''))[:200], 'score':max(0,min(100,int(c.get('score',60))))})
  except (KeyError,TypeError,ValueError): continue
 if not out: raise HTTPException(422,'A IA não retornou cortes com tempos válidos; tente outra configuração')
 return {'clips':out[:10]}

class SttReq(BaseModel):
 videoId:str
 provider:str='groq'
 baseURL:str=''
 model:str='whisper-large-v3-turbo'
 key:str=Field(min_length=4,max_length=1000)
 language:str='pt'

@app.post('/api/transcribe')
async def transcribe(r:SttReq):
 meta=get_video(r.videoId); f,_=video_paths(r.videoId)
 if not meta.get('hasAudio'): raise HTTPException(400,'O vídeo não contém áudio')
 if r.provider not in ('groq','openai','custom','riva'): raise HTTPException(400,'Selecione uma API de transcrição compatível')
 base=safe_base(r.provider,r.baseURL)
 endpoint=base if r.provider=='riva' and base.endswith('/audio/transcriptions') else base.rstrip('/')+'/audio/transcriptions'
 tmp=TEMPS/f'{uuid.uuid4()}.mp3'
 try:
  p=await asyncio.to_thread(subprocess.run,['ffmpeg','-v','error','-y','-i',str(f),'-vn','-ac','1','-ar','16000','-b:a','32k',str(tmp)],capture_output=True,timeout=240)
  if p.returncode: raise HTTPException(422,'Não foi possível extrair o áudio do vídeo')
  if tmp.stat().st_size>24*1024*1024: raise HTTPException(413,'Áudio muito longo; corte o vídeo ou ajuste os limites da API')
  data={'model':r.model,'response_format':'verbose_json'}
  if r.language: data['language']=r.language
  files={'file':('audio.mp3',tmp.read_bytes(),'audio/mpeg')}
  async with httpx.AsyncClient(timeout=200,follow_redirects=False) as client:
   try: resp=await client.post(endpoint,data=data,files=files,headers={'Authorization':f'Bearer {r.key}'})
   except httpx.RequestError as ex: raise HTTPException(502,f'Falha de conexão com transcrição: {str(ex)[:160]}')
  if resp.status_code>=400: raise HTTPException(resp.status_code,f'API de transcrição {resp.status_code}: {resp.text[:400]}')
  try: result=resp.json()
  except ValueError: raise HTTPException(502,'Resposta de transcrição não é JSON')
  seg=[]
  for x in result.get('segments',[]):
   if not isinstance(x,dict): continue
   try:
    s=float(x['start']); e=float(x['end']); t=str(x['text']).strip()
    if 0<=s<e and t: seg.append({'start':s,'end':e,'text':t})
   except (KeyError,ValueError,TypeError): continue
  if not seg:
   raise HTTPException(422,'A API retornou texto sem timestamps. Escolha um modelo que forneça segmentos com start/end ou importe SRT. Não inventamos tempos.')
  return {'segments':seg,'language':result.get('language',r.language)}
 finally: tmp.unlink(missing_ok=True)

if __name__=='__main__':
 import uvicorn
 uvicorn.run('app:app',host='127.0.0.1',port=8000)