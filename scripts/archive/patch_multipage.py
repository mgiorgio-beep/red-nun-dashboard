import base64

# PATCH 1: invoice_routes.py
py = open('/opt/rednun/invoice_routes.py').read()

old = "        # Extract data using Claude Vision\n        logger.info(f\"Processing invoice for {location}...\")\n        extracted = extract_invoice_data(image_b64, mime_type)"

new = """        # Collect any extra pages
        extra_pages = []
        i = 0
        while True:
            ef = request.files.get(f'extra_file_{i}')
            if not ef:
                break
            ef_b64 = base64.b64encode(ef.read()).decode('utf-8')
            ef_fname = ef.filename or ''
            ef_mime = 'image/jpeg'
            if ef_fname.lower().endswith('.pdf'): ef_mime = 'application/pdf'
            elif ef_fname.lower().endswith('.png'): ef_mime = 'image/png'
            elif ef_fname.lower().endswith('.heic'): ef_mime = 'image/heic'
            extra_pages.append({'data': ef_b64, 'mime': ef_mime})
            i += 1
        # Extract data using Claude Vision
        logger.info(f"Processing invoice for {location}, extra pages: {len(extra_pages)}...")
        extracted = extract_invoice_data(image_b64, mime_type, extra_pages=extra_pages)"""

count = py.count(old)
assert count == 1, f"Routes pattern count: {count}"
py = py.replace(old, new, 1)
open('/opt/rednun/invoice_routes.py', 'w').write(py)
print("invoice_routes.py patched OK")

# PATCH 2: invoice_processor.py signature
py = open('/opt/rednun/invoice_processor.py').read()

old_sig = 'def extract_invoice_data(image_base64, mime_type="image/jpeg"):'
new_sig = 'def extract_invoice_data(image_base64, mime_type="image/jpeg", extra_pages=None):'
assert py.count(old_sig) == 1, f"Sig count: {py.count(old_sig)}"
py = py.replace(old_sig, new_sig, 1)
print("Signature updated")

old_content = '                        {\n                            "type": "text",\n                            "text": prompt,\n                        },'
new_content = '                        *([{\n                            "type": "document" if p["mime"] == "application/pdf" else "image",\n                            "source": {\n                                "type": "base64",\n                                "media_type": p["mime"],\n                                "data": p["data"],\n                            },\n                        } for p in (extra_pages or [])]),\n                        {\n                            "type": "text",\n                            "text": prompt,\n                        },'

assert py.count(old_content) == 1, f"Content pattern count: {py.count(old_content)}"
py = py.replace(old_content, new_content, 1)
open('/opt/rednun/invoice_processor.py', 'w').write(py)
print("invoice_processor.py patched OK")
print("Done")
