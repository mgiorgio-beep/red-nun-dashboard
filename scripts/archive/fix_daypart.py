s = open('analytics.py','r').read()
old = "strftime('%H', opened_at)"
new = "strftime('%H', datetime(opened_at, '-5 hours'))"
s = s.replace(old, new)
old2 = "strftime('%M', opened_at)"
new2 = "strftime('%M', datetime(opened_at, '-5 hours'))"
s = s.replace(old2, new2)
open('analytics.py','w').write(s)
print('Patched - daypart now uses Eastern time')
print('Occurrences replaced:', s.count('-5 hours'))
