import json
import pyotp
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.request import urlopen

from playwright.sync_api import sync_playwright, expect
from contextlib import ExitStack
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from remote_servers import sftp_server, webdav_server
from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler, ThrottledDTPHandler
from pyftpdlib.ioloop import IOLoop
from pyftpdlib.servers import FTPServer

root = Path(tempfile.mkdtemp(prefix='nasrelay-ui-'))
remote = root / 'remote'
nas = root / 'nas'
remote.mkdir(); nas.mkdir()
(remote / '미디어').mkdir()
(remote / '미디어' / '사진.bin').write_bytes(os.urandom(2 * 1024 * 1024))
for name in ['a.txt','b.txt','c.txt']:
    (remote / name).write_text(name)
(nas / '업로드').mkdir()
(nas / '업로드' / 'NAS 파일.txt').write_text('NAS file')
auth = DummyAuthorizer()
auth.add_user('tester','test-password',str(remote),perm='elradfmwMT')
dtp = type('LimitedDTP',(ThrottledDTPHandler,),{'write_limit':512*1024,'read_limit':512*1024})
handler = type('Handler',(FTPHandler,),{'authorizer':auth,'dtp_handler':dtp})
loop = IOLoop()
ftp = FTPServer(('127.0.0.1',0),handler,ioloop=loop)
threading.Thread(target=lambda:ftp.serve_forever(timeout=.05,handle_exit=False),daemon=True).start()
ftp_port = ftp.socket.getsockname()[1]
with socket.socket() as s:
    s.bind(('127.0.0.1',0)); port = s.getsockname()[1]
env = dict(os.environ,DATA_DIR=str(root / 'data'),NAS_ROOT=str(nas))
process = subprocess.Popen([sys.executable,'-m','uvicorn','app.main:app','--host','127.0.0.1','--port',str(port)],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
url = f'http://127.0.0.1:{port}'
for _ in range(100):
    try:
        urlopen(url + '/health'); break
    except Exception: time.sleep(.05)
errors = []
try:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True,args=['--no-sandbox'])
        context = browser.new_context(viewport={'width':1440,'height':1100})
        page = context.new_page()
        page.on('pageerror',lambda err:errors.append(str(err)))
        page.goto(url)
        page.locator('#loginForm input[name=username]').fill('admin')
        page.locator('#loginForm input[name=password]').fill('admin')
        page.locator('#loginForm button[type=submit]').click()
        page.locator('#passwordDialog').wait_for(state='visible')
        assert context.request.get(url + '/api/state').status == 403
        assert page.locator('#shell').is_hidden()
        page.locator('#passwordForm input[name=current_password]').fill('admin')
        page.locator('#passwordForm input[name=new_password]').fill('browser-test-password')
        page.locator('#passwordForm input[name=confirmation]').fill('browser-test-password')
        page.locator('#passwordForm button[type=submit]').click()
        page.locator('#shell').wait_for(state='visible')
        page.locator('#connectOpen').click()
        page.locator('#connectForm input[name=host]').fill('127.0.0.1')
        page.locator('#connectForm input[name=port]').fill(str(ftp_port))
        page.locator('#connectForm input[name=username]').fill('tester')
        page.locator('#connectForm input[name=password]').fill('wrong')
        page.locator('#connectForm button[type=submit]').click()
        page.locator("#connectForm .form-error").wait_for(state="visible")
        page.locator('#connectionBadge.failed').wait_for()
        # Hold a disconnected polling response until after FTP connection succeeds.
        # The UI must wait for that poll and then fetch fresh state before browsing.
        held = []
        def hold_state(route):
            held.append((route,route.fetch()))
        page.route('**/api/state',hold_state,times=1)
        for _ in range(40):
            if held:
                break
            page.wait_for_timeout(100)
        assert held and not held[0][1].json()['connected']
        page.locator('#connectForm input[name=password]').fill('test-password')
        with page.expect_response(lambda response: response.url == url + '/api/connection' and response.request.method == 'POST') as connection:
            page.locator('#connectForm button[type=submit]').click()
        assert connection.value.status == 200
        held[0][0].fulfill(response=held[0][1])
        page.locator('#connectionBadge.connected').wait_for()
        page.locator('[data-side=ftp] .file-row').first.wait_for()
        assert page.locator('[data-side=ftp] .file-row').count() == 4
        # Ctrl multi-select.
        page.locator('[data-side=ftp] .file-row[data-path="/a.txt"] .file-name').click()
        page.locator('[data-side=ftp] .file-row[data-path="/b.txt"] .file-name').click(modifiers=['Control'])
        assert page.locator('[data-side=ftp] .file-row.selected').count() == 2
        # Shift range adds c.txt.
        page.locator('[data-side=ftp] .file-row[data-path="/c.txt"] .file-name').click(modifiers=['Shift'])
        assert page.locator('[data-side=ftp] .file-row.selected').count() == 3
        # Ctrl on a folder name selects it instead of navigating.
        page.locator('[data-side=ftp] .file-row[data-path="/미디어"] .folder-link').click(modifiers=['Control'])
        assert page.locator('[data-side=ftp] .file-row.selected').count() == 4
        # Marquee across the entire list.
        pane = page.locator('[data-side=ftp] .file-list').bounding_box()
        page.mouse.move(pane['x']+pane['width']-22,pane['y']+220)
        page.mouse.down()
        page.mouse.move(pane['x']+45,pane['y']+3,steps=12)
        page.mouse.up()
        assert page.locator('[data-side=ftp] .file-row.selected').count() == 4
        page.screenshot(path='/tmp/nasrelay-desktop.png',full_page=True)
        # Queue then close the browser tab; observe server completion separately.
        page.locator('#download').click()
        page.locator('#transferForm button[type=submit]').click()
        page.locator("#transferDialog").wait_for(state="hidden")
        page.locator('[data-action=pause]').click()
        page.locator('.job-status.paused').wait_for()
        paused = context.request.get(url + '/api/state').json()['jobs'][0]
        assert paused['status'] == 'paused'
        page.locator('[data-action=resume]').click()
        page.locator('.job-status.paused').wait_for(state='hidden')
        jobs = context.request.get(url + '/api/state').json()['jobs']
        assert any(job['status'] in ('queued','running','planning') for job in jobs), 'Expected an active transfer before tab closure'
        page.close()
        deadline = time.monotonic()+10
        while time.monotonic()<deadline and not all(job['status'] == 'succeeded' for job in context.request.get(url + '/api/state').json()['jobs']): time.sleep(.05)
        assert context.request.get(url + '/api/state').json()['jobs'][0]['status'] == 'succeeded'
        assert (nas / '미디어' / '사진.bin').read_bytes() == (remote / '미디어' / '사진.bin').read_bytes()
        assert (nas / 'a.txt').exists()
        # Reopen the page; persisted completed history remains visible.
        page = context.new_page(); page.on('pageerror',lambda err:errors.append(str(err)))
        page.goto(url); page.locator('#shell').wait_for(state='visible')
        page.locator('#navHistory').click()
        page.locator('.job-status.succeeded').wait_for()
        # Mobile checkbox selection and reverse move.
        mobile = browser.new_context(viewport={'width':390,'height':844},is_mobile=True,has_touch=True,storage_state=context.storage_state())
        phone = mobile.new_page(); phone.on('pageerror',lambda err:errors.append(str(err)))
        phone.goto(url); phone.locator('#shell').wait_for(state='visible')
        phone.locator('[data-side=nas] .file-row[data-path="/업로드"] input').check()
        assert phone.locator('[data-side=nas] .file-row.selected').count() == 1
        phone.locator('.mode-switch label').last.click()
        phone.locator('#upload').click()
        phone.locator('#transferForm button[type=submit]').click()
        deadline = time.monotonic()+10
        while time.monotonic()<deadline and (nas / '업로드').exists(): time.sleep(.05)
        assert not (nas / '업로드').exists()
        assert (remote / '업로드' / 'NAS 파일.txt').read_text() == 'NAS file'
        phone.locator('#showHistory').check()
        phone.locator('.job-status.succeeded').nth(1).wait_for()
        phone.locator('[data-side=nas] .file-row').first.wait_for()
        screenshot = phone.screenshot(path='/tmp/nasrelay-mobile.png',full_page=True)
        import struct
        assert struct.unpack('>I',screenshot[16:20])[0] == 390, 'Mobile horizontal overflow'
        # Enroll TOTP in the UI and save the five recovery codes.
        page.bring_to_front()
        page.locator('#navSecurity').click()
        page.locator('#totpSetupForm input[name=current_password]').fill('browser-test-password')
        page.locator('#totpSetupForm button[type=submit]').click()
        page.locator('#totpEnrollment').wait_for(state='visible')
        secret = page.locator('#totpKey').input_value()
        assert secret and page.locator('#totpQr').get_attribute('src').startswith('data:image/png;base64,')
        page.locator('#totpEnableForm input[name=code]').fill(pyotp.TOTP(secret).now())
        page.locator('#totpEnableForm button[type=submit]').click()
        page.locator('#recoveryDialog').wait_for(state='visible')
        codes = page.locator('#recoveryCodes code').all_text_contents()
        assert len(codes) == len(set(codes)) == 5
        with page.expect_download() as download:
            page.locator('#downloadRecovery').click()
        assert all(code in Path(download.value.path()).read_text() for code in codes)
        assert page.locator('#closeRecovery').is_disabled()
        page.locator('#recoverySaved').check()
        page.locator('#closeRecovery').click()
        page.locator('#totpDisableForm').wait_for(state='visible')
        page.locator('#securityDialog .close-dialog').click()
        page.locator('#logout').click()
        page.locator('#loginDialog').wait_for(state='visible')
        page.locator('#loginForm input[name=password]').fill('browser-test-password')
        page.locator('#loginForm button[type=submit]').click()
        page.locator('#loginCodeField').wait_for(state='visible')
        assert page.locator('#shell').is_hidden()
        page.locator('#loginForm input[name=code]').fill(codes[0])
        page.locator('#loginForm button[type=submit]').click()
        page.locator('#shell').wait_for(state='visible')
        page.locator('#navSecurity').click()
        page.locator('#totpDisableForm').wait_for(state='visible')
        assert '4개' in page.locator('#totpStatus').text_content()
        page.locator('#totpDisableForm input[name=current_password]').fill('wrong')
        page.locator('#totpDisableForm input[name=code]').fill(codes[1])
        page.locator('#totpDisableForm button[type=submit]').click()
        page.locator('#totpDisableForm .form-error').wait_for(state='visible')
        page.locator('#totpDisableForm input[name=current_password]').fill('browser-test-password')
        page.locator('#totpDisableForm button[type=submit]').click()
        page.locator('#totpSetupForm').wait_for(state='visible')
        assert page.locator('#totpBadge').text_content() == '사용 안 함'
        page.locator('#securityDialog .close-dialog').click()
        page.locator('#logout').click()
        page.locator('#loginForm input[name=password]').fill('browser-test-password')
        page.locator('#loginForm button[type=submit]').click()
        page.locator('#shell').wait_for(state='visible')
        # Save, connect, switch, edit, disconnect and delete profiles through the UI.
        original_ftp_id = page.locator('#profileSelect').input_value()
        with ExitStack() as servers:
            profiles = []
            for protocol, factory in [('webdav', webdav_server), ('sftp', sftp_server)]:
                server_root = root / protocol
                server_root.mkdir()
                (server_root / f'{protocol}.txt').write_text(protocol)
                profile = servers.enter_context(factory(server_root))
                page.locator('#newProfile').click()
                page.locator('#connectForm input[name=name]').fill(protocol + ' 테스트')
                page.locator('#connectForm select[name=protocol]').select_option(protocol)
                assert page.locator('#connectForm input[name=port]').input_value() == ('443' if protocol == 'webdav' else '22')
                if protocol == 'webdav':
                    assert page.locator('#connectForm input[name=tls]').is_checked()
                    page.locator('#connectForm input[name=tls]').uncheck()
                    assert page.locator('#connectForm input[name=port]').input_value() == '80'
                else:
                    page.locator('#connectForm input[name=host_key_sha256]').fill(profile['host_key_sha256'])
                for field in ['host','port','username','password']:
                    page.locator(f'#connectForm input[name={field}]').fill(str(profile[field]))
                page.locator('#connectForm button[type=submit]').click()
                page.locator('#connectDialog').wait_for(state='hidden')
                page.locator(f'[data-side=ftp] .file-row[data-path="/{protocol}.txt"]').wait_for()
                profile_id = page.locator('#profileSelect').input_value()
                profiles.append(profile_id)
                assert page.locator('#connectedCount').text_content() == f'{len(profiles)+1}개 연결됨'
            dav_id, sftp_id = profiles
            page.locator('#connectOpen').click()
            page.locator(f'[data-profile="{sftp_id}"] [data-profile-action=edit]').click()
            page.locator('#connectForm input[name=name]').fill('수정된 SFTP')
            page.locator('#saveProfile').click()
            page.locator('#connectDialog').wait_for(state='hidden')
            expect(page.locator('#connectionName')).to_have_text('수정된 SFTP')
            assert page.locator('#connectionBadge').get_attribute('class').endswith('connected')
            page.locator('#disconnect').click()
            expect(page.locator('#connectedCount')).to_have_text('2개 연결됨')
            assert page.locator('#connectSelected').is_enabled()
            page.locator('#connectSelected').click()
            expect(page.locator('#connectedCount')).to_have_text('3개 연결됨')
            page.locator('#profileSelect').select_option(dav_id)
            page.locator('[data-side=ftp] .file-row[data-path="/webdav.txt"]').wait_for()
            assert page.locator('[data-side=ftp] .protocol-tag').text_content() == 'WEBDAV'
            page.locator('#connectOpen').click()
            page.once('dialog', lambda dialog: dialog.accept())
            page.locator(f'[data-profile="{original_ftp_id}"] [data-profile-action=delete]').click()
            page.locator(f'[data-profile="{original_ftp_id}"]').wait_for(state='hidden')
            page.locator('#connectDialog .close-dialog').click()
            expect(page.locator('#connectedCount')).to_have_text('2개 연결됨')
            assert page.locator('#profileSelect').input_value() == dav_id
            # Save a disconnected profile without contacting its host.
            page.locator('#newProfile').click()
            for field,value in [('name','오프라인 프로필'),('host','offline.example'),('username','user')]:
                page.locator(f'#connectForm input[name={field}]').fill(value)
            page.locator('#saveProfile').click()
            page.locator('#connectDialog').wait_for(state='hidden')
            expect(page.locator('#profileSelect option')).to_have_count(3)
            assert page.locator('#profileSelect').input_value() == dav_id
            assert page.locator('#connectedCount').text_content() == '2개 연결됨'
            page.screenshot(path='/tmp/nasrelay-profiles-desktop.png',full_page=True)
            page.set_viewport_size({'width':390,'height':844})
            page.screenshot(path='/tmp/nasrelay-profiles-mobile.png',full_page=True)
            assert page.evaluate('document.documentElement.scrollWidth <= 390')
            page.set_viewport_size({'width':1440,'height':1100})
        assert not errors, errors
        browser.close()
    print(json.dumps({'browser_checks':'passed','desktop':'1440px','mobile':'390px','checks':['delayed state poll during FTP connection','admin onboarding','mandatory password change','web pause/resume','TOTP QR and key','five recovery codes','recovery download','two-factor login','recovery-code login','TOTP removal','login','connection failure/success','Ctrl selection','Shift selection','marquee selection','browser-closed transfer','history','mobile touch selection','reverse folder move','no horizontal overflow','no JavaScript errors','FTP/SFTP/WebDAV simultaneous connections','protocol defaults','profile switching','profile editing','per-server disconnect and reconnect','profile deletion','offline profile saving','profile mobile layout']},ensure_ascii=False))
finally:
    process.terminate(); process.wait(timeout=45)
    ftp.close_all(); loop.close()
