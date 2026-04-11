s = open('static/index.html', 'r').read()

old = """function apiUrl(path) {
    let url = '/api/' + path;
    if (currentLocation) url += (url.includes('?') ? '&' : '?') + 'location=' + currentLocation;
    return url;
  }"""

new = """function apiUrl(path) {
    let url = '/api/' + path;
    const { start, end } = getDateRange();
    url += (url.includes('?') ? '&' : '?') + 'start=' + start + '&end=' + end;
    if (currentLocation) url += '&location=' + currentLocation;
    return url;
  }"""

s = s.replace(old, new)
open('static/index.html', 'w').write(s)
print('DONE' if 'getDateRange' in s[s.find('apiUrl'):s.find('apiUrl')+300] else 'FAILED')
