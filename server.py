"""
PIX Monitor — Unified Server
Serves frontend (HTML/CSS/JS) + monitoring API + user API on a SINGLE port.
Designed for ngrok/cloudflared tunnel: one URL handles everything.

Port: 9102
  /                 → Frontend (index.html)
  /api/monitoring/* → Monitoring API
  /api/users/*      → User API (proxied from user-api or inline)
  /api/health       → Health check

Usage:
  python pix-monitor/server.py
  Then: ngrok http 9102
"""

import json
import os
import sys
import time
import random
import string
import hashlib
import traceback
import mimetypes
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from datetime import datetime, timezone

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

PORT = int(os.environ.get('PORT', 9102))
STATIC_DIR = os.path.dirname(os.path.abspath(__file__))

# Import monitoring API logic
sys.path.insert(0, STATIC_DIR)
from monitoring_api_core import (
    load_db, save_db, now_iso, gen_id,
    CROP_PHENOLOGY, detect_stage, compute_monitoring,
    compute_pasture_metrics, generate_report, init_gee,
    GEE_KEY_PATH, GEE_SERVICE_ACCOUNT_KEY
)

# Inline user DB (shared with user-api.py)
USER_DB_FILE = os.path.join(os.path.dirname(STATIC_DIR), 'pix-admin', 'data', 'users.db.json')

def load_users():
    if os.path.exists(USER_DB_FILE):
        with open(USER_DB_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {'users': [], 'version': 0}


class UnifiedHandler(BaseHTTPRequestHandler):

    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')

    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _error(self, msg, status=400):
        self._json({'error': msg}, status)

    def _body(self):
        length = int(self.headers.get('Content-Length', 0))
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode('utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def _serve_static(self, path):
        """Serve static files from pix-monitor directory."""
        if path == '/' or path == '':
            path = '/index.html'
        file_path = os.path.join(STATIC_DIR, path.lstrip('/'))
        if not os.path.isfile(file_path):
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'Not Found')
            return
        mime_type, _ = mimetypes.guess_type(file_path)
        with open(file_path, 'rb') as f:
            content = f.read()
        self.send_response(200)
        self.send_header('Content-Type', mime_type or 'application/octet-stream')
        self.send_header('Content-Length', len(content))
        self._cors()
        self.end_headers()
        self.wfile.write(content)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path

        # Root → index.html
        if path == '/' or path == '':
            self._serve_static('/')
            return

        path = path.rstrip('/')

        # ── API Routes ──
        if path == '/api/health':
            from monitoring_api_core import _ee_initialized
            self._json({'status': 'ok', 'service': 'pix-monitor-unified', 'port': PORT,
                        'gee': _ee_initialized, 'crops': list(CROP_PHENOLOGY.keys())})

        elif path == '/api/clients':
            db = load_db()
            self._json({'clients': db['clients']})

        elif path == '/api/fields':
            db = load_db()
            self._json({'fields': db['fields']})

        elif path == '/api/alerts':
            db = load_db()
            self._json({'alerts': db['alerts']})

        elif path == '/api/crops':
            crops = {}
            for key, cfg in CROP_PHENOLOGY.items():
                crops[key] = {
                    'name': cfg['name'], 'cycle_days': cfg['cycle_days'],
                    'stages': {k: v['desc'] for k, v in cfg['stages'].items()},
                    'critical_stages': cfg['critical_stages']
                }
            self._json({'crops': crops})

        elif path.startswith('/api/timeseries/'):
            field_id = path.split('/')[-1]
            db = load_db()
            self._json({'fieldId': field_id, 'timeseries': db.get('timeseries', {}).get(field_id, [])})

        elif path == '/api/users/sync':
            data = load_users()
            self._json({'_type': 'pix_users_sync', 'version': data.get('version', 0),
                        'updatedAt': data.get('updatedAt', ''), 'users': data.get('users', [])})

        elif path.startswith('/reports/'):
            # Serve report files (PDF, KMZ)
            self._serve_static(path)

        elif path.startswith('/api/'):
            self._error('Not found', 404)

        # ── Static files ──
        else:
            self._serve_static(path or '/')

    def do_POST(self):
        path = urlparse(self.path).path.rstrip('/')

        if path == '/api/clients':
            body = self._body()
            if not body.get('name'):
                self._error('name required')
                return
            db = load_db()
            client = {'id': gen_id('cli-'), 'name': body['name'],
                      'contact': body.get('contact', ''), 'email': body.get('email', ''),
                      'createdAt': now_iso()}
            db['clients'].append(client)
            save_db(db)
            self._json(client, 201)

        elif path == '/api/fields':
            body = self._body()
            if not body.get('name') or not body.get('boundary'):
                self._error('name and boundary required')
                return
            db = load_db()
            field = {
                'id': gen_id('field-'), 'clientId': body.get('clientId', ''),
                'name': body['name'], 'property': body.get('property', ''),
                'boundary': body['boundary'],
                'areaHa': body.get('areaHa', 0), 'crop': body.get('crop', 'soja'),
                'plantingDate': body.get('plantingDate', ''),
                'monitoring': {'active': False, 'activatedAt': None, 'lastCheck': None,
                              'currentStage': None, 'checkCount': 0},
                'createdAt': now_iso()
            }
            db['fields'].append(field)
            save_db(db)
            self._json(field, 201)

        elif path.endswith('/activate'):
            field_id = path.split('/')[-2]
            db = load_db()
            field = next((f for f in db['fields'] if f['id'] == field_id), None)
            if not field: self._error('Not found', 404); return
            field['monitoring']['active'] = True
            field['monitoring']['activatedAt'] = now_iso()
            stage_key, _, _ = detect_stage(field.get('crop', 'soja'), field.get('plantingDate', ''))
            if stage_key: field['monitoring']['currentStage'] = stage_key
            save_db(db)
            self._json({'activated': True, 'field': field})

        elif path.endswith('/check'):
            field_id = path.split('/')[-2]
            db = load_db()
            field = next((f for f in db['fields'] if f['id'] == field_id), None)
            if not field: self._error('Not found', 404); return
            try:
                result = compute_monitoring(field)
            except Exception as e:
                traceback.print_exc()
                self._error(str(e), 500); return

            # Bug 3 fix: check for GEE errors before saving
            if 'error' in result:
                self._error(result['error'], 500); return

            field['monitoring']['lastCheck'] = now_iso()
            field['monitoring']['currentStage'] = result.get('stage')
            field['monitoring']['checkCount'] = field['monitoring'].get('checkCount', 0) + 1

            if result.get('harvestDetected'):
                field['monitoring']['active'] = False
                field['monitoring']['pausedReason'] = 'harvest_detected'

            if result.get('anomalies'):
                for a in result['anomalies']:
                    db['alerts'].append(a)

            # Bug 8 fix: ensure timeseries key exists
            if 'timeseries' not in db:
                db['timeseries'] = {}
            ts_key = field['id']
            if ts_key not in db['timeseries']:
                db['timeseries'][ts_key] = []
            db['timeseries'][ts_key].append({
                'date': now_iso(), 'stage': result.get('stage'),
                'values': result.get('currentValues', {}),
                'zScore': result.get('zScore'), 'images': result.get('imagesFound', 0),
                'cloudBlocked': result.get('cloudBlocked', False)
            })
            save_db(db)

            # ── AUTO REPORT: Generate PDF + send email if data obtained ──
            if not result.get('cloudBlocked') and result.get('currentValues'):
                has_data = any(v is not None for v in result.get('currentValues', {}).values())
                if has_data:
                    try:
                        client = next((c for c in db['clients'] if c['id'] == field.get('clientId')), None)
                        field_alerts = [a for a in db['alerts'] if a.get('fieldId') == field_id and a.get('status') == 'active']
                        ts = db.get('timeseries', {}).get(field_id, [])
                        report = generate_report(field, client, field_alerts, ts)
                        result['report'] = report

                        # Auto-send email to client if email exists
                        client_email = client.get('email', '') if client else ''
                        if client_email and '@' in client_email:
                            import threading
                            def send_report_email(email, report_data, field_data, result_data):
                                try:
                                    import smtplib
                                    from email.mime.multipart import MIMEMultipart
                                    from email.mime.text import MIMEText
                                    from email.mime.base import MIMEBase
                                    from email import encoders

                                    smtp_user = os.environ.get('SMTP_USER', '')
                                    smtp_pass = os.environ.get('SMTP_PASS', '')
                                    if not smtp_user or not smtp_pass:
                                        print(f'[Email] SMTP not configured — skipping email to {email}')
                                        return

                                    vals = result_data.get('currentValues', {})
                                    health = report_data.get('health', '?')
                                    stage_desc = result_data.get('stageDesc', '?')
                                    primary = result_data.get('primaryIndex', 'NDVI')

                                    html = f"""<div style="font-family:Arial;max-width:600px;margin:0 auto;background:#0a1220;color:#F1F5F9;padding:20px;border-radius:12px">
<h1 style="color:#7FD633;font-size:18px;text-align:center">PIX Monitor — Informe Automatico</h1>
<p style="color:#94A3B8;font-size:11px;text-align:center">Sentinel-2 | Google Earth Engine | Indices Israel</p>
<div style="background:#162236;padding:12px;border-radius:8px;margin:10px 0;border-left:4px solid {'#22C55E' if health in ['EXCELENTE','BUENO'] else '#F5A623' if health=='MODERADO' else '#EF4444'}">
<h2 style="color:{'#22C55E' if health in ['EXCELENTE','BUENO'] else '#F5A623' if health=='MODERADO' else '#EF4444'};font-size:14px;margin:0 0 6px 0">ESTADO: {health}</h2>
<p style="font-size:12px;margin:2px 0"><b>Lote:</b> {field_data.get('name','')} ({field_data.get('areaHa',0)} ha)</p>
<p style="font-size:12px;margin:2px 0"><b>Etapa:</b> {stage_desc}</p>
<p style="font-size:12px;margin:2px 0"><b>Indice primario:</b> {primary}</p>
<p style="font-size:12px;margin:2px 0"><b>Imagen:</b> {result_data.get('imageDate','?')}</p>
</div>
<div style="background:#162236;padding:12px;border-radius:8px;margin:10px 0">
<h3 style="color:#7FD633;font-size:12px;margin:0 0 6px 0">INDICES</h3>
<table style="width:100%;font-size:11px;color:#E2E8F0;border-collapse:collapse">"""
                                    for k in ['TCARI_OSAVI','SIF_proxy','CWSI','SALINITY','NDVI','NDRE','NDMI','PSRI']:
                                        v = vals.get(k)
                                        if v is not None:
                                            html += f'<tr style="background:#0F1B2D"><td style="padding:3px">{k}</td><td style="text-align:center">{v:.4f}</td></tr>'
                                    html += f"""</table></div>
<div style="text-align:center;margin:12px 0">
<a href="{report_data.get('googleMapsLink','#')}" style="background:#7FD633;color:#000;padding:8px 16px;border-radius:6px;text-decoration:none;font-weight:bold;font-size:12px">Navegar al Lote</a>
</div>
<p style="color:#64748B;font-size:9px;text-align:center">Pixadvisor — www.pixadvisor.network</p>
</div>"""

                                    msg = MIMEMultipart()
                                    msg['From'] = smtp_user
                                    msg['To'] = email
                                    msg['Subject'] = f'PIX Monitor — {field_data.get("name","")} — {health}'
                                    msg.attach(MIMEText(html, 'html'))

                                    # Attach PDF if exists
                                    pdf_path = report_data.get('pdfPath', '')
                                    if pdf_path and os.path.exists(pdf_path):
                                        with open(pdf_path, 'rb') as f:
                                            part = MIMEBase('application', 'pdf')
                                            part.set_payload(f.read())
                                            encoders.encode_base64(part)
                                            part.add_header('Content-Disposition', f'attachment; filename="{report_data.get("pdf","report.pdf")}"')
                                            msg.attach(part)

                                    server = smtplib.SMTP('smtp.gmail.com', 587)
                                    server.starttls()
                                    server.login(smtp_user, smtp_pass)
                                    server.send_message(msg)
                                    server.quit()
                                    print(f'[Email] Sent to {email}: {field_data.get("name","")} — {health}')
                                except Exception as e:
                                    print(f'[Email] Error sending to {email}: {e}')

                            threading.Thread(target=send_report_email, args=(client_email, report, field, result), daemon=True).start()
                            print(f'[Auto] Report generated + email queued to {client_email}')
                        else:
                            print(f'[Auto] Report generated (no client email)')
                    except Exception as e:
                        print(f'[Auto] Report error: {e}')

            self._json(result)

        elif path.endswith('/pause'):
            field_id = path.split('/')[-2]
            db = load_db()
            field = next((f for f in db['fields'] if f['id'] == field_id), None)
            if not field: self._error('Not found', 404); return
            field['monitoring']['active'] = False
            field['monitoring']['pausedReason'] = 'admin_manual'
            save_db(db)
            self._json({'paused': True})

        elif path.endswith('/resume'):
            field_id = path.split('/')[-2]
            db = load_db()
            field = next((f for f in db['fields'] if f['id'] == field_id), None)
            if not field: self._error('Not found', 404); return
            field['monitoring']['active'] = True
            field['monitoring']['pausedReason'] = None
            save_db(db)
            self._json({'resumed': True})

        elif path.startswith('/api/reports/'):
            field_id = path.split('/')[-1]
            db = load_db()
            field = next((f for f in db['fields'] if f['id'] == field_id), None)
            if not field: self._error('Not found', 404); return
            client = next((c for c in db['clients'] if c['id'] == field.get('clientId')), None)
            alerts = [a for a in db['alerts'] if a.get('fieldId') == field_id and a.get('status') == 'active']
            ts = db.get('timeseries', {}).get(field_id, [])
            try:
                result = generate_report(field, client, alerts, ts)
                self._json(result)
            except Exception as e:
                self._error(str(e), 500)

        else:
            self._error('Not found', 404)

    def do_PUT(self):
        path = urlparse(self.path).path.rstrip('/')
        if path.startswith('/api/fields/'):
            field_id = path.split('/')[-1]
            body = self._body()
            db = load_db()
            field = next((f for f in db['fields'] if f['id'] == field_id), None)
            if not field: self._error('Not found', 404); return
            for k in ['name', 'crop', 'plantingDate', 'areaHa', 'clientId', 'property']:
                if k in body: field[k] = body[k]
            if 'boundary' in body: field['boundary'] = body['boundary']
            save_db(db)
            self._json(field)
        else:
            self._error('Not found', 404)

    def do_DELETE(self):
        path = urlparse(self.path).path.rstrip('/')
        if path.startswith('/api/fields/'):
            field_id = path.split('/')[-1]
            db = load_db()
            db['fields'] = [f for f in db['fields'] if f['id'] != field_id]
            db['alerts'] = [a for a in db['alerts'] if a.get('fieldId') != field_id]
            if field_id in db.get('timeseries', {}): del db['timeseries'][field_id]
            save_db(db)
            self._json({'deleted': True})
        elif path.startswith('/api/clients/'):
            client_id = path.split('/')[-1]
            db = load_db()
            db['clients'] = [c for c in db['clients'] if c['id'] != client_id]
            save_db(db)
            self._json({'deleted': True})
        elif path.startswith('/api/alerts/'):
            alert_id = path.split('/')[-1]
            db = load_db()
            db['alerts'] = [a for a in db['alerts'] if a['id'] != alert_id]
            save_db(db)
            self._json({'deleted': True})
        else:
            self._error('Not found', 404)

    def log_message(self, format, *args):
        pass


if __name__ == '__main__':
    print('=== PIX Monitor — Unified Server ===')
    print(f'Port: {PORT}')
    print(f'Frontend: {STATIC_DIR}')
    gee_available = os.path.exists(GEE_KEY_PATH) or bool(GEE_SERVICE_ACCOUNT_KEY)
    print(f'GEE: {"OK" if gee_available else "NOT FOUND"}')
    print(f'Users DB: {USER_DB_FILE}')
    print()

    if gee_available:
        init_gee()

    db = load_db()
    print(f'Data: {len(db["clients"])} clients, {len(db["fields"])} fields, {len(db["alerts"])} alerts')
    print(f'')
    print(f'Local:  http://localhost:{PORT}/')
    print(f'To expose online: ngrok http {PORT}')
    print()

    server = HTTPServer(('0.0.0.0', PORT), UnifiedHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nShutting down...')
        server.server_close()
