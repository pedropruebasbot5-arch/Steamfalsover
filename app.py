import hmac
import json
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from uuid import uuid4

from flask import Flask, abort, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SNEAM_SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("SNEAM_HTTPS") == "1",
)

DATABASE = Path(os.environ.get("SNEAM_DATABASE", Path(__file__).with_name("sneam.db")))
app.config["MAX_CONTENT_LENGTH"] = 6 * 1024 * 1024
PAYMENT_METHODS = {"tarjeta": "Tarjeta (demostración)", "paypal": "PayPal (demostración)", "spei": "SPEI (demostración)"}
ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

JUEGOS = [
    {"id": 1, "titulo": "CYBERPUNK 2077: Phantom Liberty", "precio": 499, "desc": "Futuro distópico, mundo abierto.", "tag": "RPG", "descuento": 50, "img": "https://images.unsplash.com/photo-1511512578047-dfb367046420?q=80&w=600"},
    {"id": 2, "titulo": "Elden Ring", "precio": 899, "desc": "El GOTY de mundo abierto de FromSoftware.", "tag": "Souls", "descuento": 0, "img": "https://images.unsplash.com/photo-1538481199705-c710c4e965fc?q=80&w=600"},
    {"id": 3, "titulo": "Hogwarts Legacy", "precio": 749, "desc": "Vive tu vida como mago.", "tag": "Aventura", "descuento": 30, "img": "https://images.unsplash.com/photo-1518709268805-4e9042af9f23?q=80&w=600"},
    {"id": 4, "titulo": "Baldur's Gate 3", "precio": 999, "desc": "El RPG definitivo del año.", "tag": "RPG", "descuento": 0, "img": "https://images.unsplash.com/photo-1493711662062-fa541adb3fc8?q=80&w=600"},
    {"id": 5, "titulo": "HELLDIVERS 2", "precio": 450, "desc": "Co-op galáctico para liberar planetas.", "tag": "Acción", "descuento": 20, "img": "https://images.unsplash.com/photo-1542751371-adc38448a05e?q=80&w=600"},
    {"id": 6, "titulo": "Red Dead Redemption 2", "precio": 599, "desc": "Épica del salvaje oeste.", "tag": "Mundo Abierto", "descuento": 67, "img": "https://images.unsplash.com/photo-1550745165-9bc0b252726f?q=80&w=600"},
]


def get_db():
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


@contextmanager
def db_connection():
    connection = get_db()
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db():
    DATABASE.parent.mkdir(parents=True, exist_ok=True)
    with db_connection() as db:
        products_table_exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'products'"
        ).fetchone() is not None
        db.execute("""
            CREATE TABLE IF NOT EXISTS offers (
                game_id INTEGER PRIMARY KEY,
                discount INTEGER NOT NULL CHECK (discount BETWEEN 0 AND 90)
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                library_token TEXT NOT NULL,
                customer_name TEXT NOT NULL,
                email TEXT NOT NULL,
                payment_method TEXT NOT NULL,
                games_json TEXT NOT NULL,
                total INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                titulo TEXT NOT NULL,
                precio INTEGER NOT NULL CHECK (precio >= 0),
                description TEXT NOT NULL,
                tag TEXT NOT NULL,
                img TEXT NOT NULL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        order_columns = {row["name"] for row in db.execute("PRAGMA table_info(orders)")}
        if "user_id" not in order_columns:
            db.execute("ALTER TABLE orders ADD COLUMN user_id INTEGER REFERENCES users(id)")
        db.execute("""
            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
                comment TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(product_id, user_id)
            )
        """)
        if not products_table_exists:
            db.executemany(
                "INSERT INTO products (id, titulo, precio, description, tag, img) VALUES (?, ?, ?, ?, ?, ?)",
                [(game["id"], game["titulo"], game["precio"], game["desc"], game["tag"], game["img"])
                 for game in JUEGOS],
            )


def get_games():
    with db_connection() as db:
        discounts = {row["game_id"]: row["discount"] for row in db.execute("SELECT game_id, discount FROM offers")}
        products = db.execute("SELECT id, titulo, precio, description, tag, img FROM products ORDER BY id").fetchall()
    return [
        {
            "id": game["id"],
            "titulo": game["titulo"],
            "precio": game["precio"],
            "desc": game["description"],
            "tag": game["tag"],
            "img": game["img"],
            "descuento": discounts.get(game["id"], next(
                (default["descuento"] for default in JUEGOS if default["id"] == game["id"]), 0
            )),
        }
        for game in products
    ]


def product_image_value(upload, image_url, current_image=""):
    if upload and upload.filename:
        extension = Path(upload.filename).suffix.lower()
        if extension not in ALLOWED_IMAGE_EXTENSIONS:
            abort(400, description="La imagen debe ser JPG, PNG, WEBP o GIF.")
        upload_dir = Path(app.static_folder) / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{uuid4().hex}{extension}"
        upload.save(upload_dir / filename)
        return url_for("static", filename=f"uploads/{filename}")

    image_url = image_url.strip()
    if not image_url:
        image_url = current_image
    if not image_url or len(image_url) > 1000 or not (
        image_url.startswith(("https://", "http://", "/static/", "static/"))
    ):
        abort(400, description="Indica una URL de imagen http(s) o sube un archivo de imagen.")
    return image_url


def product_form_values():
    title = request.form.get("titulo", "").strip()
    description = request.form.get("description", "").strip()
    tag = request.form.get("tag", "").strip()
    try:
        price = int(request.form.get("precio", ""))
    except ValueError:
        abort(400, description="El precio debe ser un número entero válido.")
    if not 1 <= len(title) <= 100 or len(description) > 500 or not 1 <= len(tag) <= 40 or price < 0:
        abort(400, description="Revisa el nombre, precio, descripción y categoría del producto.")
    return title, price, description, tag


def get_csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


app.jinja_env.globals["csrf_token"] = get_csrf_token


def require_admin(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin"))
        return view(*args, **kwargs)
    return wrapped


@app.before_request
def protect_post_requests():
    if request.method != "POST":
        return
    supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token", "")
    expected = session.get("csrf_token", "")
    if not expected or not hmac.compare_digest(supplied, expected):
        if request.path.startswith("/api/"):
            return jsonify(error="La sesión expiró. Recarga la página e inténtalo de nuevo."), 400
        abort(400, description="La sesión expiró. Recarga la página e inténtalo de nuevo.")


@app.route("/")
def home():
    games = get_games()
    with db_connection() as db:
        reviews = db.execute(
            """SELECT reviews.product_id, reviews.rating, reviews.comment, reviews.created_at, users.name
               FROM reviews JOIN users ON users.id = reviews.user_id
               ORDER BY reviews.created_at DESC"""
        ).fetchall()
    reviews_by_game = {}
    for review in reviews:
        reviews_by_game.setdefault(review["product_id"], []).append(dict(review))
    for game in games:
        game_reviews = reviews_by_game.get(game["id"], [])
        game["reviews"] = game_reviews[:3]
        game["review_count"] = len(game_reviews)
        game["average_rating"] = round(sum(item["rating"] for item in game_reviews) / len(game_reviews), 1) if game_reviews else 0
    return render_template("index.html", juegos=games, ofertas=[game for game in games if game["descuento"] > 0])


@app.route("/juego/<int:game_id>")
def game_detail(game_id):
    games = get_games()
    game = next((item for item in games if item["id"] == game_id), None)
    if game is None:
        abort(404, description="No encontramos ese juego.")

    with db_connection() as db:
        reviews = db.execute(
            """SELECT reviews.rating, reviews.created_at, users.name
               FROM reviews JOIN users ON users.id = reviews.user_id
               WHERE reviews.product_id = ?
               ORDER BY reviews.created_at DESC""",
            (game_id,),
        ).fetchall()

    game["review_count"] = len(reviews)
    game["average_rating"] = round(sum(review["rating"] for review in reviews) / len(reviews), 1) if reviews else 0
    similar_games = [item for item in games if item["id"] != game_id and item["tag"].casefold() == game["tag"].casefold()]
    if len(similar_games) < 3:
        similar_games.extend(item for item in games if item["id"] != game_id and item not in similar_games)

    return render_template(
        "game.html",
        juego=game,
        resenas=reviews,
        similares=similar_games[:4],
    )


@app.route("/cuenta", methods=["GET", "POST"])
def account():
    if request.method == "POST":
        mode = request.form.get("mode", "login")
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if len(email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            return render_template("account.html", error="Escribe un correo electrónico válido.", mode=mode), 400
        if len(password) > 128:
            return render_template("account.html", error="La contraseña no puede superar 128 caracteres.", mode=mode), 400

        if mode == "register":
            name = request.form.get("name", "").strip()
            if not 2 <= len(name) <= 100 or len(password) < 8:
                return render_template("account.html", error="Escribe tu nombre y una contraseña de al menos 8 caracteres.", mode=mode), 400
            try:
                with db_connection() as db:
                    db.execute(
                        "INSERT INTO users (name, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
                        (name, email, generate_password_hash(password), datetime.now(timezone.utc).isoformat(timespec="seconds")),
                    )
                    user = db.execute("SELECT id, name, email FROM users WHERE email = ?", (email,)).fetchone()
            except sqlite3.IntegrityError:
                return render_template("account.html", error="Ya existe una cuenta con ese correo.", mode=mode), 409
        elif mode == "login":
            with db_connection() as db:
                stored_user = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
            if stored_user is None or not check_password_hash(stored_user["password_hash"], password):
                return render_template("account.html", error="Correo o contraseña incorrectos.", mode=mode), 401
            user = stored_user
        else:
            abort(400, description="Acción de cuenta no válida.")

        guest_token = session.get("library_token")
        session.clear()
        session["user_id"] = user["id"]
        session["user_name"] = user["name"]
        session["user_email"] = user["email"]
        if guest_token:
            with db_connection() as db:
                db.execute("UPDATE orders SET user_id = ? WHERE library_token = ? AND user_id IS NULL", (user["id"], guest_token))
        return redirect(url_for("account"))

    if session.get("user_id"):
        return render_template("account.html", user={
            "name": session.get("user_name", ""),
            "email": session.get("user_email", ""),
        })
    return render_template("account.html", mode=request.args.get("modo", "login"))


@app.route("/cuenta/salir", methods=["POST"])
def account_logout():
    session.pop("user_id", None)
    session.pop("user_name", None)
    session.pop("user_email", None)
    session.pop("library_token", None)
    return redirect(url_for("home"))


@app.route("/productos/<int:game_id>/resenas", methods=["POST"])
def create_review(game_id):
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("account", modo="login"))
    try:
        rating = int(request.form.get("rating", ""))
    except ValueError:
        abort(400, description="La calificación debe ser de 1 a 5 estrellas.")
    if game_id not in {game["id"] for game in get_games()}:
        abort(404, description="El producto no existe.")
    if not 1 <= rating <= 5:
        abort(400, description="La calificación debe ser de 1 a 5 estrellas.")
    with db_connection() as db:
        db.execute(
            """INSERT INTO reviews (product_id, user_id, rating, comment, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(product_id, user_id) DO UPDATE SET
                   rating = excluded.rating, comment = excluded.comment, created_at = excluded.created_at""",
            (game_id, user_id, rating, "", datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
    if request.form.get("return_to") == "detail":
        return redirect(url_for("game_detail", game_id=game_id))
    return redirect(url_for("home", _anchor=f"producto-{game_id}"))


@app.route("/api/juegos")
def api_juegos():
    return jsonify(get_games())


@app.route("/api/checkout", methods=["POST"])
def checkout():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        data = {}
    name = str(data.get("name", "")).strip()
    email = str(data.get("email", "")).strip().lower()
    method = data.get("payment_method")
    game_ids = data.get("game_ids")

    if not 2 <= len(name) <= 100:
        return jsonify(error="Escribe tu nombre (de 2 a 100 caracteres)."), 400
    if len(email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return jsonify(error="Escribe un correo electrónico válido."), 400
    if not isinstance(method, str) or method not in PAYMENT_METHODS:
        return jsonify(error="Selecciona una forma de pago disponible."), 400
    games_by_id = {game["id"]: game for game in get_games()}
    if not isinstance(game_ids, list) or not 1 <= len(game_ids) <= len(games_by_id):
        return jsonify(error="El carrito está vacío o no es válido."), 400
    if any(type(game_id) is not int for game_id in game_ids) or len(set(game_ids)) != len(game_ids):
        return jsonify(error="El carrito contiene juegos no válidos o repetidos."), 400

    if any(game_id not in games_by_id for game_id in game_ids):
        return jsonify(error="Uno de los juegos ya no está disponible."), 400
    purchased_games = [games_by_id[game_id] for game_id in game_ids]
    total = sum(game["precio"] * (100 - game["descuento"]) // 100 for game in purchased_games)
    order_id = uuid4().hex
    library_token = session.setdefault("library_token", secrets.token_urlsafe(32))
    with db_connection() as db:
        db.execute(
            """INSERT INTO orders
                    (order_id, library_token, user_id, customer_name, email, payment_method, games_json, total, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (order_id, library_token, session.get("user_id"), name, email, PAYMENT_METHODS[method],
             json.dumps([game["id"] for game in purchased_games]),
             total, datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )

    return jsonify(order_id=order_id, total=total, library_url=url_for("library")), 201


@app.route("/biblioteca")
def library():
    orders = []
    if session.get("user_id"):
        with db_connection() as db:
            orders = db.execute(
                "SELECT order_id, games_json, total, created_at FROM orders WHERE user_id = ? ORDER BY created_at DESC",
                (session["user_id"],),
            ).fetchall()
    elif session.get("library_token"):
        with db_connection() as db:
            orders = db.execute(
                "SELECT order_id, games_json, total, created_at FROM orders WHERE library_token = ? AND user_id IS NULL ORDER BY created_at DESC",
                (session["library_token"],),
            ).fetchall()
    games_by_id = {game["id"]: game for game in get_games()}
    purchases = [
        {
            "order_id": order["order_id"],
            "total": order["total"],
            "created_at": order["created_at"],
            "games": [games_by_id[game_id] for game_id in json.loads(order["games_json"]) if game_id in games_by_id],
        }
        for order in orders
    ]
    return render_template("library.html", compras=purchases)


@app.route("/admin", methods=["GET", "POST"])
def admin():
    if request.method == "POST":
        challenge = session.pop("admin_challenge", None)
        try:
            answer = int(request.form.get("answer", ""))
        except ValueError:
            answer = None
        if not challenge or answer != sum(challenge):
            challenge = [secrets.randbelow(10) + 1, secrets.randbelow(10) + 1]
            session["admin_challenge"] = challenge
            return render_template(
                "admin.html",
                login_error=True,
                challenge_left=challenge[0],
                challenge_right=challenge[1],
            ), 401
        session.clear()
        session["is_admin"] = True
        return redirect(url_for("admin"))

    if not session.get("is_admin"):
        challenge = [secrets.randbelow(10) + 1, secrets.randbelow(10) + 1]
        session["admin_challenge"] = challenge
        return render_template(
            "admin.html",
            challenge_left=challenge[0],
            challenge_right=challenge[1],
        )

    games = get_games()
    games_by_id = {game["id"]: game for game in games}
    with db_connection() as db:
        summary = db.execute(
            "SELECT COUNT(*) AS order_count, COALESCE(SUM(total), 0) AS revenue FROM orders"
        ).fetchone()
        orders = db.execute(
            "SELECT order_id, customer_name, email, payment_method, games_json, total, created_at FROM orders ORDER BY created_at DESC LIMIT 100"
        ).fetchall()
    sales = [
        {
            **dict(order),
            "games": [games_by_id[game_id]["titulo"] if game_id in games_by_id else f"Producto eliminado (#{game_id})"
                      for game_id in json.loads(order["games_json"])],
        }
        for order in orders
    ]
    return render_template(
        "admin.html",
        is_admin=True,
        juegos=games,
        ventas=sales,
        total_ventas=summary["order_count"],
        ingresos=summary["revenue"],
    )


@app.route("/admin/ofertas", methods=["POST"])
@require_admin
def update_offer():
    try:
        game_id = int(request.form.get("game_id", ""))
        discount = int(request.form.get("discount", ""))
    except ValueError:
        abort(400, description="El juego y el descuento deben ser válidos.")
    if game_id not in {game["id"] for game in get_games()} or not 0 <= discount <= 90:
        abort(400, description="El descuento debe estar entre 0 y 90%.")
    with db_connection() as db:
        db.execute(
            "INSERT INTO offers (game_id, discount) VALUES (?, ?) ON CONFLICT(game_id) DO UPDATE SET discount = excluded.discount",
            (game_id, discount),
        )
    return redirect(url_for("admin", _anchor="ofertas"))


@app.route("/admin/productos/crear", methods=["POST"])
@require_admin
def create_product():
    title, price, description, tag = product_form_values()
    image = product_image_value(request.files.get("image"), request.form.get("image_url", ""))
    with db_connection() as db:
        db.execute(
            "INSERT INTO products (titulo, precio, description, tag, img) VALUES (?, ?, ?, ?, ?)",
            (title, price, description, tag, image),
        )
    return redirect(url_for("admin", _anchor="productos"))


@app.route("/admin/productos/<int:game_id>/actualizar", methods=["POST"])
@require_admin
def update_product(game_id):
    title, price, description, tag = product_form_values()
    with db_connection() as db:
        current = db.execute("SELECT img FROM products WHERE id = ?", (game_id,)).fetchone()
        if current is None:
            abort(404, description="El producto ya no existe.")
        image = product_image_value(request.files.get("image"), request.form.get("image_url", ""), current["img"])
        db.execute(
            "UPDATE products SET titulo = ?, precio = ?, description = ?, tag = ?, img = ? WHERE id = ?",
            (title, price, description, tag, image, game_id),
        )
    return redirect(url_for("admin", _anchor="productos"))


@app.route("/admin/productos/<int:game_id>/eliminar", methods=["POST"])
@require_admin
def delete_product(game_id):
    with db_connection() as db:
        result = db.execute("DELETE FROM products WHERE id = ?", (game_id,))
        if result.rowcount == 0:
            abort(404, description="El producto ya no existe.")
        db.execute("DELETE FROM offers WHERE game_id = ?", (game_id,))
    return redirect(url_for("admin", _anchor="productos"))


@app.route("/admin/salir", methods=["POST"])
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("admin"))


init_db()

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "5000")),
        debug=os.environ.get("FLASK_DEBUG") == "1",
    )
