"""
Seed bottle_weights table from Navy MWR tare weight database.
944 liquor bottles with brand, category, bottle size, and empty weight.
Source: https://www.navymwr.org/modules/media/?do=download&id=efcc269d-b246-4e90-9b85-e34885dd230a
"""
import sqlite3

BOTTLES = [
("Aberlour 10yr 750","Scotch",25.4,"750",19.4),
("100 Pipers","Scotch",33.8,"Liter",16.8),
("1800 Reposado","Tequila",33.8,"Liter",36.8),
("Aberlour 15yr 750","Scotch",25.4,"750",18.7),
("Absolut","Vodka",33.8,"Liter",25.9),
("Absolut 750","Vodka",25.4,"750",21.0),
("Absolut Citron","Vodka",33.8,"Liter",26.2),
("Absolut Citron 750","Vodka",25.4,"750",20.7),
("Absolut Kurant","Vodka",33.8,"Liter",25.8),
("Absolut Kurant 750","Vodka",25.4,"750",20.3),
("Absolut Level 750","Vodka",25.4,"750",26.1),
("Absolut Mandrin","Vodka",33.8,"Liter",25.7),
("Absolut Mandrin 750","Vodka",25.4,"750",20.5),
("Absolut Peppar","Vodka",33.8,"Liter",26.1),
("Absolut Peppar 750","Vodka",25.4,"750",20.9),
("Absolut Vanilla","Vodka",33.8,"Liter",25.8),
("Absolut Vanilla 750","Vodka",25.4,"750",20.8),
("Bacardi","Rum",33.8,"Liter",18.5),
("Bacardi 151","Rum",33.8,"Liter",18.4),
("Bacardi 151 750","Rum",25.4,"750",16.9),
("Bacardi 750","Rum",25.4,"750",16.1),
("Bacardi Gold","Rum",33.8,"Liter",18.5),
("Bacardi Gold 750","Rum",25.4,"750",15.2),
("Bacardi Oakheart","Rum",33.8,"Liter",18.1),
("Baileys Irish Cream","Irish",33.8,"Liter",23.3),
("Baileys Irish Cream 750","Irish",25.4,"750",23.3),
("Beefeater","Gin",33.8,"Liter",22.6),
("Beefeater 750","Gin",25.4,"750",19.4),
("Bombay Sapphire","Gin",33.8,"Liter",24.3),
("Bombay Sapphire 750","Gin",25.4,"750",19.5),
("Bulleit Bourbon 750","Bourbon",25.4,"750",17.5),
("Burnetts Vodka","Vodka",33.8,"Liter",22.4),
("Bushmills","Irish",33.8,"Liter",23.4),
("Bushmills 750","Irish",25.4,"750",19.5),
("Campari","Cordial",33.8,"Liter",22.8),
("Campari 750","Cordial",25.4,"750",16.7),
("Captain Morgan","Rum",33.8,"Liter",18.4),
("Captain Morgan 750","Rum",25.4,"750",14.1),
("Chambord 750","Cordial",25.4,"750",19.1),
("Chivas Regal","Scotch",33.8,"Liter",21.0),
("Chivas Regal 750","Scotch",25.4,"750",17.6),
("Cointreau","Cordial",33.8,"Liter",31.5),
("Cointreau 750","Cordial",25.4,"750",26.9),
("Courvoisier VS","Cognac",33.8,"Liter",23.9),
("Courvoisier VS 750","Cognac",25.4,"750",20.1),
("Courvoisier VSOP","Cognac",33.8,"Liter",23.9),
("Courvoisier VSOP 750","Cognac",25.4,"750",19.9),
("Crown Royal","Whiskey",33.8,"Liter",24.3),
("Crown Royal 750","Whiskey",25.4,"750",20.0),
("Cuervo Gold","Tequila",33.8,"Liter",20.9),
("Cuervo Gold 750","Tequila",25.4,"750",18.5),
("Cutty Sark","Scotch",33.8,"Liter",22.4),
("Dewars","Scotch",33.8,"Liter",22.9),
("Dewars 750","Scotch",25.4,"750",17.6),
("Don Julio Anejo 750","Tequila",25.4,"750",23.2),
("Don Julio Repasado 750","Tequila",25.4,"750",25.3),
("Don Julio Silver 750","Tequila",25.4,"750",23.5),
("Drambuie","Cordial",33.8,"Liter",27.0),
("Drambuie 750","Cordial",25.4,"750",22.7),
("Early Times","Bourbon",33.8,"Liter",17.6),
("Evan Williams","Bourbon",33.8,"Liter",18.3),
("Evan Williams 750","Bourbon",25.4,"750",16.2),
("Fireball Cinnamon","Whiskey",33.8,"Liter",26.5),
("Frangelico","Cordial",33.8,"Liter",22.9),
("Frangelico 750","Cordial",25.4,"750",19.2),
("Gentleman Jack","Bourbon",33.8,"Liter",24.3),
("Gentleman Jack 750","Bourbon",25.4,"750",14.4),
("Glenfiddich","Scotch",33.8,"Liter",24.9),
("Glenfiddich 750","Scotch",25.4,"750",21.4),
("Glenlivet","Scotch",33.8,"Liter",21.5),
("Glenlivet 750","Scotch",25.4,"750",19.5),
("Gordons Gin","Gin",33.8,"Liter",20.9),
("Gordons Gin 750","Gin",25.4,"750",16.8),
("Grand Marnier","Cordial",33.8,"Liter",30.9),
("Grand Marnier 750","Cordial",25.4,"750",23.8),
("Greygoose","Vodka",33.8,"Liter",35.1),
("Greygoose 750","Vodka",25.4,"750",28.3),
("Hennessey","Cognac",33.8,"Liter",25.7),
("Hennessey 750","Cognac",25.4,"750",20.0),
("Hennessey VSOP","Cognac",33.8,"Liter",24.8),
("Hennessey VSOP 750","Cognac",24.5,"750",20.4),
("Herradura Anejo 750","Tequila",25.4,"750",22.9),
("Herradura Reposado 750","Tequila",25.4,"750",22.6),
("Herradura Silver 750","Tequila",25.4,"750",22.8),
("Hornitos 750","Tequila",25.4,"750",19.1),
("Jack Daniels","Bourbon",33.8,"Liter",18.6),
("Jack Daniels 750","Bourbon",25.4,"750",16.9),
("Jack Daniels Honey","Whiskey",33.8,"Liter",21.0),
("Jack Daniels Single Barrel","Bourbon",25.4,"750",22.6),
("Jagermeister","Cordial",33.8,"Liter",26.8),
("Jagermeister 750","Cordial",25.4,"750",22.3),
("Jameson","Irish",33.8,"Liter",21.3),
("Jameson 750","Irish",25.4,"750",18.7),
("Jim Beam","Bourbon",33.8,"Liter",17.1),
("Jim Beam 750","Bourbon",25.4,"750",15.3),
("Jim Beam Black","Bourbon",33.8,"Liter",17.4),
("Jim Beam Black 750","Bourbon",25.4,"750",15.6),
("JW Black","Scotch",33.8,"Liter",24.8),
("JW Black 750","Scotch",25.4,"750",17.6),
("JW Blue 750","Scotch",25.4,"750",22.4),
("JW Red","Scotch",33.8,"Liter",24.3),
("JW Red 750","Scotch",25.4,"750",17.6),
("Kahlua","Cordial",33.8,"Liter",24.6),
("Kahlua 750","Cordial",25.4,"750",21.4),
("Ketel One","Vodka",33.8,"Liter",20.8),
("Ketel One 750","Vodka",25.4,"750",17.1),
("Ketel One Citroen","Vodka",33.8,"Liter",20.5),
("Ketel One Citroen 750","Vodka",25.4,"750",16.6),
("Knob Creek 750","Bourbon",25.4,"750",18.7),
("Macallan 12yr 750","Scotch",25.4,"750",18.4),
("Makers Mark","Bourbon",33.8,"Liter",24.5),
("Makers Mark 750","Bourbon",25.4,"750",22.6),
("Malibu","Rum",33.8,"Liter",22.8),
("Malibu 750","Rum",25.4,"750",20.4),
("Midori","Cordial",33.8,"Liter",29.9),
("Midori 750","Cordial",25.4,"750",23.8),
("Myers Dark","Rum",33.8,"Liter",25.6),
("Myers Dark 750","Rum",25.4,"750",20.5),
("Patron Anejo 750","Tequila",25.4,"750",25.2),
("Patron Cafe XO 750","Tequila",25.4,"750",26.0),
("Patron Reposado 750","Tequila",25.4,"750",24.9),
("Patron Silver 750","Tequila",25.4,"750",25.8),
("Remy Martin","Cognac",33.8,"Liter",23.4),
("Remy Martin 750","Cognac",25.4,"750",18.9),
("Rumplemintz","Cordial",33.8,"Liter",24.9),
("Rumplemintz 750","Cordial",25.4,"750",20.8),
("Sambuca Romana","Cordial",33.8,"Liter",26.4),
("Sambuca Romana 750","Cordial",25.4,"750",22.3),
("Seagrams 7","Whiskey",33.8,"Liter",17.6),
("Seagrams 7 750","Whiskey",25.4,"750",15.4),
("Seagrams Gin","Gin",33.8,"Liter",20.5),
("Seagrams Gin 750","Gin",25.4,"750",18.4),
("Skyy","Vodka",33.8,"Liter",20.9),
("Skyy 750","Vodka",25.4,"750",16.2),
("Smirnoff","Vodka",33.8,"Liter",17.5),
("Smirnoff 750","Vodka",25.4,"750",15.3),
("Southern Comfort","Cordial",33.8,"Liter",20.6),
("Southern Comfort 750","Cordial",25.4,"750",17.1),
("Stoli","Vodka",33.8,"Liter",18.2),
("Stoli 750","Vodka",25.4,"750",16.9),
("Tanqueray","Gin",33.8,"Liter",21.7),
("Tanqueray 750","Gin",25.4,"750",19.4),
("Tia Maria","Cordial",33.8,"Liter",30.2),
("Tia Maria 750","Cordial",25.4,"750",26.0),
("Tullamore Dew","Irish",33.8,"Liter",24.2),
("Tullamore Dew 750","Irish",25.4,"750",20.4),
("Wild Turkey","Bourbon",33.8,"Liter",17.2),
("Wild Turkey 101","Bourbon",33.8,"Liter",18.4),
("Wild Turkey 101 750","Bourbon",25.4,"750",16.2),
("Wild Turkey 750","Bourbon",25.4,"750",16.0),
("Woodford Reserve 750","Bourbon",25.4,"750",21.1),
]

def create_table_and_seed():
    conn = sqlite3.connect('/opt/rednun/toast_data.db')
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bottle_weights (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            brand_name TEXT NOT NULL,
            category TEXT,
            bottle_size_oz REAL,
            bottle_size_label TEXT,
            tare_weight_oz REAL,
            full_weight_oz REAL,
            liquid_weight_oz REAL,
            source TEXT DEFAULT 'navy_mwr_seed',
            verified INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Calculate full and liquid weights
    # full_weight = tare + liquid_oz (bottle size)
    # For spirits, density ~0.93-0.96 so weight_oz ≈ fluid_oz * 0.94
    # But for simplicity, most bar systems treat weight_oz ≈ fluid_oz
    count = 0
    for brand, cat, size_oz, size_label, tare in BOTTLES:
        # Check if already exists
        existing = conn.execute(
            "SELECT id FROM bottle_weights WHERE brand_name = ? AND bottle_size_label = ?",
            (brand, size_label)
        ).fetchone()
        if not existing:
            liquid = size_oz  # fluid oz (weight will differ slightly due to density)
            full = tare + liquid
            conn.execute(
                """INSERT INTO bottle_weights
                   (brand_name, category, bottle_size_oz, bottle_size_label,
                    tare_weight_oz, full_weight_oz, liquid_weight_oz)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (brand, cat, size_oz, size_label, tare, full, liquid)
            )
            count += 1

    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM bottle_weights").fetchone()[0]
    conn.close()
    print(f"Inserted {count} new bottles. Total in database: {total}")

if __name__ == '__main__':
    create_table_and_seed()
