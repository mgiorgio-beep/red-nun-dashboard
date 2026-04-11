s = open('data_store.py', 'r', encoding='utf-8').read()

# Fix: Add DELETE before inserting order items
old = '''            # THEN insert line items
            for item_data in items_to_insert:
                cursor.execute("""
                    INSERT INTO order_items
                    (order_guid, check_guid, location, business_date,
                     item_guid, item_name, quantity, price, discount, voided, tax)'''

new = '''            # THEN insert line items - delete old ones first to avoid duplicates
            cursor.execute("DELETE FROM order_items WHERE order_guid = ?", (guid,))
            for item_data in items_to_insert:
                cursor.execute("""
                    INSERT INTO order_items
                    (order_guid, check_guid, location, business_date,
                     item_guid, item_name, quantity, price, discount, voided, tax)'''

if old in s:
    s = s.replace(old, new)
    open('data_store.py', 'w', encoding='utf-8').write(s)
    print('FIXED data_store.py - order_items now deduplicates on sync')
else:
    print('ERROR - pattern not found')
    idx = s.find('insert line items')
    if idx >= 0:
        print(repr(s[idx:idx+300]))

# Now clean up existing duplicates
print('\nCleaning duplicate order_items...')
import sqlite3
conn = sqlite3.connect('analytics.db')
before = conn.execute('SELECT COUNT(*) FROM order_items').fetchone()[0]

# Keep only one copy of each item per order
conn.execute('''
    DELETE FROM order_items WHERE rowid NOT IN (
        SELECT MIN(rowid) FROM order_items
        GROUP BY order_guid, check_guid, item_guid, item_name, price, quantity
    )
''')
conn.commit()

after = conn.execute('SELECT COUNT(*) FROM order_items').fetchone()[0]
conn.close()
print(f'Before: {before} rows')
print(f'After:  {after} rows')
print(f'Removed: {before - after} duplicates')
