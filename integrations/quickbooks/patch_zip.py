#!/usr/bin/env python3
"""Patch email_invoice_poller.py to handle zip attachments from ScanSnap."""
content = open('/opt/red-nun-dashboard/email_invoice_poller.py').read()

# Find the exact string to replace
search = "        if len(data) < 1000:\n            continue  # skip tiny icons / signatures\n        attachments.append({"

if search not in content:
    # Try to find it
    idx = content.find("skip tiny icons")
    print(f"Context around 'skip tiny icons':")
    print(repr(content[idx-100:idx+200]))
else:
    old = """        if len(data) < 1000:
            continue  # skip tiny icons / signatures
        attachments.append({"""
    new = """        if len(data) < 1000:
            continue  # skip tiny icons / signatures
        # Unzip if needed — ScanSnap sends JPEGs inside a zip
        if ext == 'zip' or mime_type in ('application/zip', 'application/x-zip-compressed') or filename.lower().endswith('.zip'):
            try:
                import zipfile, io as _io
                with zipfile.ZipFile(_io.BytesIO(data)) as zf:
                    for zname in sorted(zf.namelist()):
                        if zname.lower().endswith(('.jpg', '.jpeg')):
                            zdata = zf.read(zname)
                            attachments.append({
                                'data': zdata,
                                'mime_type': 'image/jpeg',
                                'filename': zname,
                                'ext': 'jpg',
                            })
                            logger.info(f'  Unzipped: {zname} ({len(zdata)} bytes)')
                continue
            except Exception as e:
                logger.warning(f'  Failed to unzip {filename}: {e}')
                continue
        attachments.append({"""
    content = content.replace(old, new)
    open('/opt/red-nun-dashboard/email_invoice_poller.py', 'w').write(content)
    print('Done')
