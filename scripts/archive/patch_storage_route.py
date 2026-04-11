"""
Quick patch to add /storage route to server.py
Run: python3 patch_storage_route.py
"""

with open('server.py', 'r') as f:
    content = f.read()

if '/storage' in content and 'storage.html' in content:
    print("Already has /storage route, skipping")
else:
    route = '''
@app.route("/storage")
def storage_page():
    """Serve the storage layout interface."""
    return send_from_directory("static", "storage.html")
'''
    # Add after /count route or /manage route
    if 'count.html' in content:
        content = content.replace(
            '    return send_from_directory("static", "count.html")',
            '    return send_from_directory("static", "count.html")\n' + route
        )
    else:
        content = content.replace(
            '    return send_from_directory("static", "manage.html")',
            '    return send_from_directory("static", "manage.html")\n' + route
        )
    
    with open('server.py', 'w') as f:
        f.write(content)
    print("✅ Added /storage route to server.py")

print("Done!")
