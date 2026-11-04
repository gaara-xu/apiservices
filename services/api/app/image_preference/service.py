"""数据库图片索引、人工选择与可恢复文件移动。"""
import json
import os
import uuid
from contextlib import contextmanager
from pathlib import Path
from fastapi import HTTPException
from ..db import connect, audit
from .files import EXTENSIONS, safe_path, location_key, choose_destination, perform_move

SERVICE = 'image-preference'
STATES = {'all':'pending', 'good':'good', 'like':'like', 'notlike':'notlike', 'ai_like':'ai_like', 'ai_notlike':'ai_notlike'}


def folder_config():
    """从MySQL读取人工与AI复核目录配置，并确认存在且相互独立。"""
    with connect() as conn, conn.cursor() as cur:
        cur.execute('SELECT value FROM service_settings WHERE service=%s AND setting_key=%s', (SERVICE, 'folders'))
        row = cur.fetchone()
    if not row:
        raise HTTPException(503, '尚未配置图片目录')
    values = json.loads(row['value']) if isinstance(row['value'], str) else row['value']
    roots = {key: Path(values[key]).resolve() for key in STATES}
    for key, root in roots.items():
        if not root.is_dir():
            raise HTTPException(503, '图片目录不存在：' + str(root))
        if any(root == other or root.is_relative_to(other) for name, other in roots.items() if name != key):
            raise HTTPException(503, '图片目录不能相同或互相嵌套')
    return roots


@contextmanager
def locked_connection():
    """跨进程串行化扫描、恢复和文件移动，避免重复点击与升级并发。"""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT GET_LOCK('apiserver_image_review', 2) AS acquired")
            if cur.fetchone()['acquired'] != 1:
                raise HTTPException(423, '图片目录正在处理，请稍后重试')
        try:
            yield conn
        finally:
            with conn.cursor() as cur:
                cur.execute("SELECT RELEASE_LOCK('apiserver_image_review')")


def finalize_move(conn, operation):
    """文件移动完成后原子记录归属及审计，人工AI复核同时登记一次增量学习请求。"""
    with conn.cursor() as cur:
        cur.execute('SELECT * FROM image_items WHERE id=%s', (operation['image_id'],))
        before = cur.fetchone()
        cur.execute('UPDATE image_items SET folder=%s,root_path=%s,relative_path=%s,location_key=%s,state=%s WHERE id=%s',
            (operation['decision'], operation['destination_root'], operation['destination_relative'],
             location_key(operation['destination_root'], operation['destination_relative']), operation['decision'], operation['image_id']))
        cur.execute("UPDATE image_moves SET state='completed',completed_at=CURRENT_TIMESTAMP,error=NULL WHERE id=%s", (operation['id'],))
        audit(cur, SERVICE, 'image', operation['image_id'], 'move_completed', before,
              {'decision': operation['decision'], 'destination': operation['destination_path'], 'operation_id': operation['id']})
        if before['folder'] in ('ai_like','ai_notlike') and operation['decision'] in ('like','notlike'):
            cur.execute('INSERT IGNORE INTO image_learning_requests(id,image_id,label) VALUES(%s,%s,%s)',
                        (operation['id'],operation['image_id'],operation['decision']))
            if cur.rowcount:
                audit(cur,SERVICE,'learning_request',operation['id'],'human_review',None,
                      {'image_id':operation['image_id'],'label':operation['decision']})
        conn.commit()


def recover_moves(conn):
    """恢复已提交意图但未完成数据库确认的移动，失败留待检查且不丢弃源文件。"""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM image_moves WHERE state='prepared' ORDER BY created_at,id")
        operations = cur.fetchall()
    for operation in operations:
        try:
            perform_move(operation)
        except (OSError, ValueError) as exc:
            with conn.cursor() as cur:
                cur.execute("UPDATE image_moves SET state='failed',error=%s WHERE id=%s", (str(exc), operation['id']))
                cur.execute('SELECT folder FROM image_items WHERE id=%s', (operation['image_id'],))
                original = cur.fetchone()
                state = STATES[original['folder']] if Path(operation['source_path']).exists() and not Path(operation['destination_path']).exists() else 'error'
                cur.execute('UPDATE image_items SET state=%s WHERE id=%s', (state, operation['image_id']))
                audit(cur, SERVICE, 'image', operation['image_id'], 'move_failed', None, {'error': str(exc), 'operation_id': operation['id']})
                conn.commit()
        else:
            finalize_move(conn, operation)


def scan(folders=None):
    """扫描指定目录并登记元数据；可仅检查人工样本，避免学习时扫描整个待筛图片库。"""
    roots = folder_config()
    if folders is not None:
        roots = {key:roots[key] for key in folders}
    token = str(uuid.uuid4())
    totals = {}
    with locked_connection() as conn:
        recover_moves(conn)
        with conn.cursor() as cur:
            for folder, root in roots.items():
                count = 0
                for directory, dirs, files in os.walk(root):
                    dirs[:] = sorted(name for name in dirs if not (Path(directory)/name).is_symlink())
                    for name in sorted(files):
                        path = Path(directory) / name
                        if path.suffix.lower() not in EXTENSIONS or path.is_symlink():
                            continue
                        try:
                            stat = path.stat()
                        except FileNotFoundError:
                            continue
                        relative = str(path.relative_to(root))
                        cur.execute('INSERT INTO image_items(id,location_key,folder,root_path,relative_path,state,size_bytes,mtime_ns,seen_token) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE size_bytes=VALUES(size_bytes),mtime_ns=VALUES(mtime_ns),seen_token=VALUES(seen_token),state=IF(state IN (\'missing\',\'pending\',\'good\',\'like\',\'notlike\'),VALUES(state),state)',
                            (str(uuid.uuid4()), location_key(root, relative), folder, str(root), relative, STATES[folder], stat.st_size, stat.st_mtime_ns, token))
                        count += 1
                cur.execute("UPDATE image_items SET state='missing' WHERE root_path=%s AND seen_token<>%s AND state NOT IN ('moving','error')", (str(root), token))
                totals[folder] = count
            audit(cur, SERVICE, 'scan', token, 'scan', None, {'counts':totals, 'roots':{key:str(path) for key,path in roots.items()}})
            conn.commit()
    return {'scanned':totals, 'ai_used':False}


def stats():
    """按当前配置目录统计待选、已选及异常图片数量。"""
    roots = folder_config()
    counts = {state: 0 for state in ('pending','good','like','notlike','ai_like','ai_notlike','missing','error','moving')}
    with connect() as conn, conn.cursor() as cur:
        cur.execute('SELECT state,COUNT(*) AS amount FROM image_items WHERE root_path IN (' + ','.join(['%s'] * len(roots)) + ') GROUP BY state', tuple(str(root) for root in roots.values()))
        for row in cur.fetchall():
            counts[row['state']] = row['amount']
    return counts


def public_item(row):
    """返回图片标识及原始文件字节数，供页面显示文件大小。"""
    return {'id':row['id'], 'name':row['relative_path'], 'state':row['state'], 'size_bytes':row['size_bytes'],
            'preview_url':f"/api/v1/image-preference/images/{row['id']}/preview",
            'content_url':f"/api/v1/image-preference/images/{row['id']}/content"}


def review_progress(cur, roots, source, has_image):
    """按当前AI复核目录统计已完成与待处理数量，刷新页面后进度仍然保留。"""
    if source=='all':return None
    keys=['ai_like','ai_notlike'] if source=='ai' else [source]
    clauses=' OR '.join('(folder=%s AND root_path=%s AND state=%s)' for _ in keys)
    params=tuple(value for key in keys for value in (key,str(roots[key]),STATES[key]))
    cur.execute('SELECT COUNT(*) AS amount FROM image_items WHERE '+clauses,params)
    remaining=cur.fetchone()['amount']
    sources=' OR '.join("LEFT(source_path,CHAR_LENGTH(%s)+1)=CONCAT(%s,'/')" for _ in keys)
    params=tuple(value for key in keys for value in (str(roots[key]),str(roots[key])))
    cur.execute("SELECT COUNT(*) AS amount FROM image_moves WHERE state='completed' AND decision IN ('like','notlike') AND ("+sources+")",params)
    completed=cur.fetchone()['amount']
    return {'completed':completed,'remaining':remaining,'total':completed+remaining,
            'position':completed+1 if has_image else completed}


def next_image(source="all"):
    """返回固定五图窗口：最近两张人工处理记录及前三张待处理图，缺位保持为空。"""
    roots = folder_config()
    keys = ['all'] if source == 'all' else ['ai_like', 'ai_notlike'] if source == 'ai' else [source]
    with locked_connection() as conn:
        recover_moves(conn)
        with conn.cursor() as cur:
            clauses = ' OR '.join('(folder=%s AND root_path=%s AND state=%s)' for _ in keys)
            params = tuple(value for key in keys for value in (key, str(roots[key]), STATES[key]))
            while True:
                cur.execute('SELECT * FROM image_items WHERE (' + clauses + ') ORDER BY created_at,id LIMIT 3', params)
                pending = cur.fetchall()
                missing = [row for row in pending if not safe_path(row['root_path'], row['relative_path']).is_file()]
                if not missing:
                    break
                for row in missing:
                    cur.execute("UPDATE image_items SET state='missing' WHERE id=%s", (row['id'],))
                    audit(cur, SERVICE, 'image', row['id'], 'missing', row, {'state':'missing'})
                conn.commit()
            sources = ' OR '.join("LEFT(m.source_path,CHAR_LENGTH(%s)+1)=CONCAT(%s,'/')" for _ in keys)
            history_params = tuple(value for key in keys for value in (str(roots[key]), str(roots[key])))
            cur.execute("SELECT i.*,MAX(m.completed_at) AS reviewed_at FROM image_moves m JOIN image_items i ON i.id=m.image_id WHERE m.state='completed' AND m.decision IN ('like','notlike') AND (" + sources + ") AND i.state IN ('like','notlike') GROUP BY i.id ORDER BY reviewed_at DESC,i.id DESC LIMIT 2", history_params)
            history = [public_item(row) for row in cur.fetchall() if row['folder'] in roots and row['root_path'] == str(roots[row['folder']]) and safe_path(row['root_path'],row['relative_path']).is_file()]
            history.reverse()
            upcoming = [public_item(row) for row in pending]
            return {'image':upcoming[0] if upcoming else None,
                    'window':[None]*(2-len(history))+history+upcoming+[None]*(3-len(upcoming)),
                    'review_progress':review_progress(cur,roots,source,bool(upcoming))}


def image_path(image_id):
    """只提供已索引且位于当前配置目录内的图片读取路径。"""
    roots = folder_config()
    with connect() as conn, conn.cursor() as cur:
        cur.execute('SELECT * FROM image_items WHERE id=%s', (image_id,))
        row = cur.fetchone()
    if not row or row['folder'] not in roots or row['root_path'] != str(roots[row['folder']]):
        raise HTTPException(404, '图片不存在')
    try:
        path = safe_path(row['root_path'], row['relative_path'])
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not path.is_file():
        raise HTTPException(404, '图片已移动或不存在，请刷新')
    return path


def decide(image_id, decision, ai=False):
    """按人工确认或AI预测移动图片，拒绝AI覆盖人工标签，并支持同目标重试。"""
    roots = folder_config()
    with locked_connection() as conn:
        recover_moves(conn)
        with conn.cursor() as cur:
            cur.execute('SELECT * FROM image_items WHERE id=%s', (image_id,))
            row = cur.fetchone()
            if not row or row['root_path'] not in [str(path) for path in roots.values()]:
                raise HTTPException(404, '图片不存在')
            if row['state'] == decision:
                return {'image':public_item(row), 'already_applied':True}
            allowed = ('all',) if ai else ('all', 'ai_like', 'ai_notlike')
            if row['folder'] not in allowed or row['state'] != STATES[row['folder']]:
                raise HTTPException(409, '图片已处理或处于异常状态，请刷新')
            try:
                source = safe_path(roots[row['folder']], row['relative_path'])
                stat = source.stat()
                if (stat.st_size, stat.st_mtime_ns) != (row['size_bytes'],row['mtime_ns']):
                    raise ValueError('图片已被修改，请重新扫描目录')
                target = choose_destination(roots[decision], row['relative_path'], image_id)
                relative = str(target.relative_to(roots[decision]))
                cur.execute('SELECT id FROM image_items WHERE location_key=%s', (location_key(roots[decision], relative),))
                if cur.fetchone():
                    renamed = str(Path(relative).with_name(target.stem + '__' + str(uuid.uuid4()) + target.suffix))
                    target = choose_destination(roots[decision], renamed, image_id)
            except (OSError, ValueError) as exc:
                raise HTTPException(409, str(exc)) from exc
            operation = {'id':str(uuid.uuid4()), 'image_id':image_id, 'source_path':str(source), 'destination_path':str(target),
                'destination_root':str(roots[decision]), 'destination_relative':str(target.relative_to(roots[decision])), 'decision':decision,
                'source_device':stat.st_dev, 'source_inode':stat.st_ino, 'source_size':stat.st_size, 'source_mtime_ns':stat.st_mtime_ns}
            cur.execute('INSERT INTO image_moves(id,image_id,source_path,destination_path,destination_root,destination_relative,decision,source_device,source_inode,source_size,source_mtime_ns) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)', tuple(operation.values()))
            cur.execute("UPDATE image_items SET state='moving' WHERE id=%s", (image_id,))
            audit(cur, SERVICE, 'image', image_id, 'move_prepared', row, operation)
            conn.commit()
        recover_moves(conn)
        with conn.cursor() as cur:
            cur.execute('SELECT * FROM image_moves WHERE id=%s', (operation['id'],))
            result = cur.fetchone()
            if result['state'] != 'completed':
                raise HTTPException(409, result['error'] or '移动尚未完成')
            cur.execute('SELECT * FROM image_items WHERE id=%s', (image_id,))
            return {'image':public_item(cur.fetchone()), 'already_applied':False}


def accept_upload(temporary, filename):
    """原子发布上传原图并登记待初筛索引，同名文件自动改名且记录审计。"""
    root = folder_config()['all']
    with locked_connection() as conn:
        image_id = str(uuid.uuid4())
        target = choose_destination(root, filename, image_id)
        stat = temporary.stat()
        with conn.cursor() as cur:
            relative = str(target.relative_to(root))
            cur.execute('SELECT id FROM image_items WHERE location_key=%s', (location_key(root,relative),))
            if cur.fetchone():
                target = target.with_name(target.stem+'__'+image_id+target.suffix)
                relative = target.name
            row = {'id':image_id,'relative_path':relative,'state':'pending','size_bytes':stat.st_size}
            cur.execute('INSERT INTO image_items(id,location_key,folder,root_path,relative_path,state,size_bytes,mtime_ns,seen_token) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)', (image_id,location_key(root,relative),'all',str(root),relative,'pending',stat.st_size,stat.st_mtime_ns,str(uuid.uuid4())))
            audit(cur,SERVICE,'image',image_id,'upload',None,row)
            os.link(temporary,target)
            conn.commit()
        return {'image':public_item(row)}
