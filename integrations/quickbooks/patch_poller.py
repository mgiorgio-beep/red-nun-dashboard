#!/usr/bin/env python3
content = open('/opt/rednun/email_invoice_poller.py').read()

old = '        saved += 1\n\n    return saved'
new = '''        saved += 1
        # Auto-scan via API
        try:
            import requests
            with open(filepath, 'rb') as img:
                mime = att['mime_type']
                resp = requests.post(
                    'http://127.0.0.1:8080/api/invoices/scan',
                    files={'file': (filename, img, mime)},
                    data={'location': location},
                    timeout=120
                )
            if resp.status_code == 200:
                result = resp.json()
                status = result.get('status', 'unknown')
                vendor = (result.get('data') or {}).get('vendor_name', '?')
                logger.info(f'  OCR complete: {vendor} [{status}]')
            elif resp.status_code == 409:
                logger.info(f'  Duplicate invoice — skipped')
            else:
                logger.warning(f'  OCR failed: {resp.status_code} {resp.text[:100]}')
        except Exception as e:
            logger.error(f'  OCR error: {e}')
    return saved'''

if old not in content:
    print('ERROR: target string not found')
    print('Looking for:')
    print(repr(old))
    # Show context around 'return saved'
    idx = content.find('return saved')
    print('Found "return saved" at index:', idx)
    print('Context:', repr(content[idx-50:idx+20]))
else:
    open('/opt/rednun/email_invoice_poller.py', 'w').write(content.replace(old, new))
    print('Done')
