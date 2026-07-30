import sqlite3

conn = sqlite3.connect(r"D:\tgbot\orders.db")
cursor = conn.cursor()

cursor.execute("""
    SELECT id, name, max_photo_ids, max_post_id, is_active
    FROM products
    WHERE max_photo_ids IS NOT NULL
    ORDER BY id DESC
    LIMIT 10
""")

rows = cursor.fetchall()
if rows:
    for row in rows:
        print(f"ID: {row[0]}, Название: {row[1]}, "
              f"Фото: {'есть' if row[2] else 'нет'}, "
              f"Опубликован: {row[3] or 'нет'}, Активен: {row[4]}")
else:
    print("Ни у одного товара нет max_photo_ids.")
conn.close()