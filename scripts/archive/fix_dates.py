s = open('static/index.html', 'r', encoding='utf-8').read()

old = "function apiUrl(path) {\n    let url = '/api/' + path;\n    if (currentLocation) url += (url.includes('?') ? '&' : '?') + 'location=' + currentLocation;\n    return url;\n  }"

new = "function apiUrl(path) {\n    let url = '/api/' + path;\n    const { start, end } = getDateRange();\n    url += (url.includes('?') ? '&' : '?') + 'start=' + start + '&end=' + end;\n    if (currentLocation) url += '&location=' + currentLocation;\n    return url;\n  }"

if old in s:
    s = s.replace(old, new)
    open('static/index.html', 'w', encoding='utf-8').write(s)
    print('FIXED - apiUrl now includes date range')
else:
    print('ERROR - could not find apiUrl pattern')
    idx = s.find('function apiUrl')
    if idx >= 0:
        print('Found at index', idx)
        print(repr(s[idx:idx+300]))
    else:
        print('apiUrl not found at all!')
