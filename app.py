import os
import io
import uuid
import datetime
import logging
import time
import base64
import json
import requests
from flask import Flask, render_template, request, redirect, url_for, flash
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from PIL import Image, ImageOps
from supabase import create_client, Client

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'super_secret_key_for_passport_generator')
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # 5MB limit
app.config['UPLOAD_EXTENSIONS'] = ['.jpg', '.jpeg', '.png']

# Setup Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize Firebase

supabase: Client = None

REMOVE_BG_API_URL = "https://api.remove.bg/v1.0/removebg"
DEFAULT_REMOVE_BG_API_KEY = os.environ.get("REMOVE_BG_API_KEY", "").strip()
REMOVE_BG_API_KEY_FILE = os.environ.get("REMOVE_BG_API_KEY_FILE", "remove_bg_api_key.txt")
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
PASSPORT_SIZE = (413, 531)  # 35x45mm at 300 DPI

def get_remove_bg_api_key():
    """Return remove.bg API key. Prioritizes local file, then environment variable."""
    try:
        key_file_path = os.path.join(PROJECT_DIR, REMOVE_BG_API_KEY_FILE)
        if os.path.exists(key_file_path):
            with open(key_file_path, "r", encoding="utf-8-sig") as f:
                file_key = (f.read() or "").strip()
                if file_key:
                    return file_key
    except Exception:
        logger.exception("Failed reading REMOVE_BG_API_KEY_FILE")
        
    api_key = os.environ.get("REMOVE_BG_API_KEY", "").strip()
    if api_key:
        return api_key
        
    if DEFAULT_REMOVE_BG_API_KEY:
        return DEFAULT_REMOVE_BG_API_KEY
        
    return ""

try:
    supabase_url = os.environ.get("SUPABASE_URL", "")
    supabase_key = os.environ.get("SUPABASE_KEY", "")
    
    # Fallback to local config file
    local_config_file = os.path.join(PROJECT_DIR, "supabase_key.json")
    if (not supabase_url or not supabase_key) and os.path.exists(local_config_file):
        try:
            with open(local_config_file, 'r') as f:
                config = json.load(f)
                supabase_url = config.get("SUPABASE_URL", supabase_url)
                supabase_key = config.get("SUPABASE_KEY", supabase_key)
        except Exception as e:
            logger.error(f"Failed to read supabase_key.json: {e}")

    if supabase_url and supabase_key:
        supabase = create_client(supabase_url, supabase_key)
    else:
        logger.warning("Supabase URL or Key not found in environment variables or supabase_key.json")
except Exception as e:
    logger.error(f"Supabase initialization error: {e}")

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

class User(UserMixin):
    def __init__(self, user_id, username, email):
        self.id = user_id
        self.username = username
        self.email = email

@login_manager.user_loader
def load_user(user_id):
    if not supabase:
        return None
    try:
        res = supabase.table('users').select('*').eq('id', user_id).execute()
        if res.data:
            data = res.data[0]
            # Use email prefix if username is not in schema
            uname = data.get('username', data.get('email', '').split('@')[0])
            return User(user_id=data.get('id'), username=uname, email=data.get('email'))
    except Exception as e:
        logger.error(f"Error loading user: {e}")
    return None

def process_image(input_image_bytes, bg_color):
    """Removes background, applies color, and resizes to passport size."""
    start_time = time.time()
    # 1. Fix EXIF orientation (crucial for mobile photos)
    original = Image.open(io.BytesIO(input_image_bytes))
    original = ImageOps.exif_transpose(original)
    
    # Convert to PNG bytes before calling remove.bg
    temp_io = io.BytesIO()
    original.save(temp_io, format="PNG")
    fixed_bytes = temp_io.getvalue()
    
    # 2. Remove background via remove.bg API
    # Read API key at request-time so the server works even if env vars change
    # after app startup.
    api_key = get_remove_bg_api_key()
    if not api_key:
        raise RuntimeError(
            "Missing remove.bg API key. Set env var `REMOVE_BG_API_KEY` or create "
            f"`{REMOVE_BG_API_KEY_FILE}` in the project folder."
        )
    try:
        response = requests.post(
            REMOVE_BG_API_URL,
            headers={"X-Api-Key": api_key},
            files={"image_file": ("upload.png", fixed_bytes, "image/png")},
            data={"size": "auto", "type": "person"},
            timeout=120,
        )
    except requests.RequestException as e:
        raise RuntimeError(f"remove.bg request failed: {e}") from e

    if response.status_code != 200:
        error_detail = "No response body"
        try:
            error_json = response.json()
            errors = error_json.get("errors", [])
            if errors:
                first_error = errors[0]
                error_detail = f"{first_error.get('code', 'api_error')}: {first_error.get('title', 'Unknown error')}"
            else:
                error_detail = str(error_json)[:300]
        except ValueError:
            if response.text:
                error_detail = response.text[:300]
        raise RuntimeError(f"remove.bg API error {response.status_code}: {error_detail}")

    no_bg_bytes = response.content
    if not no_bg_bytes or len(no_bg_bytes) < 50:
        raise RuntimeError("remove.bg returned empty/invalid output.")

    # Debug: write intermediate images so we can verify background removal.
    try:
        with open("debug_api_no_bg.png", "wb") as f:
            f.write(no_bg_bytes)
    except Exception:
        # Debug writing must not break the main flow.
        pass
    no_bg_img = Image.open(io.BytesIO(no_bg_bytes)).convert("RGBA")
    
    # Calculate background color
    color_map = {
        'white': (255, 255, 255, 255),
        'blue': (0, 0, 255, 255),  # Adjust if you want specific passport blue, e.g., (173, 216, 230, 255)
        'red': (255, 0, 0, 255)
    }
    bg_rgba = color_map.get(bg_color, (255, 255, 255, 255))
    
    target_size = PASSPORT_SIZE
    final_img = Image.new("RGBA", target_size, bg_rgba)
    
    # Resize foreground while keeping aspect ratio.
    # Fit within ~92% width and ~88% height of passport canvas.
    fit_box = (int(target_size[0] * 0.92), int(target_size[1] * 0.88))
    no_bg_img.thumbnail(fit_box, Image.Resampling.LANCZOS)
    
    # Center and place subject slightly above bottom for typical passport framing.
    x = (target_size[0] - no_bg_img.width) // 2
    y = int(target_size[1] * 0.96) - no_bg_img.height
    y = max(0, y)
    
    final_img.paste(no_bg_img, (x, y), no_bg_img)
    
    # Convert back to bytes
    output = io.BytesIO()
    final_img.convert("RGB").save(output, format="JPEG", quality=95, dpi=(300, 300))
    logger.info("Image processed in %.2f seconds", time.time() - start_time)
    final_bytes = output.getvalue()
    try:
        with open("debug_processed.jpg", "wb") as f:
            f.write(final_bytes)
    except Exception:
        pass
    return final_bytes

# --- Supabase Helper Functions ---
def upload_to_supabase(file_bytes, user_id):
    """Uploads image to Supabase Storage and returns public URL and blob path."""
    if not supabase:
        raise RuntimeError("Supabase not configured")
    filename = f"{uuid.uuid4()}.jpg"
    blob_path = f"{user_id}/{filename}"
    try:
        res = supabase.storage.from_("photos").upload(
            blob_path,
            file_bytes,
            file_options={"content-type": "image/jpeg", "upsert": "false"}
        )
        logger.info(f"Storage upload response: {res}")
    except Exception as upload_err:
        logger.error(f"Storage upload error detail: {upload_err}")
        raise
    public_url = supabase.storage.from_("photos").get_public_url(blob_path)
    return public_url, blob_path

def save_photo(user_id, image_url, blob_path=None):
    """Saves photo metadata to Supabase."""
    if not supabase:
        raise RuntimeError("Supabase not configured")
    photo_id = str(uuid.uuid4())
    supabase.table('photos').insert({
        'id': photo_id,
        'user_id': user_id,
        'image_url': image_url,
        'created_at': datetime.datetime.now(datetime.timezone.utc).isoformat()
    }).execute()
    return photo_id

def get_user_photos(user_id):
    """Fetches user photos from Supabase."""
    if not supabase:
        return []
    res = supabase.table('photos').select('*').eq('user_id', user_id).order('created_at', desc=True).execute()
    return res.data

def delete_photo_from_supabase(photo_id, user_id):
    """Deletes photo from Supabase Database and Storage."""
    if not supabase:
        return False, "Supabase not configured."
        
    res = supabase.table('photos').select('*').eq('id', photo_id).execute()
    if not res.data:
        return False, "Photo not found."
        
    photo_data = res.data[0]
    if photo_data.get('user_id') != user_id:
        return False, "Unauthorized action."
        
    try:
        image_url = photo_data.get('image_url', '')
        blob_path = image_url.split('/public/photos/')[-1] if '/public/photos/' in image_url else None
        
        if blob_path:
            supabase.storage.from_("photos").remove([blob_path])
        supabase.table('photos').delete().eq('id', photo_id).execute()
        return True, "Photo deleted successfully."
    except Exception as e:
        logger.error(f"Delete error: {e}")
        return False, "An error occurred while deleting the photo."

@app.route('/')
def home():
    return render_template('home.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
        
    if request.method == 'POST':
        username = request.form.get('username')
        email = request.form.get('email')
        password = request.form.get('password')
        
        if not supabase:
            flash("Supabase not configured. Cannot register.", "danger")
            return redirect(url_for('register'))
        
        try:
            res = supabase.table('users').select('*').eq('email', email).execute()
            if res and hasattr(res, 'data') and res.data:
                flash("Email already registered.", "danger")
                return redirect(url_for('register'))
                
            user_id = str(uuid.uuid4())
            hashed_password = generate_password_hash(password)
            
            supabase.table('users').insert({
                'id': user_id,
                'email': email,
                'password_hash': hashed_password,
                'created_at': datetime.datetime.now(datetime.timezone.utc).isoformat()
            }).execute()
            
            flash("Registration successful. Please log in.", "success")
            return redirect(url_for('login'))
        except Exception as e:
            logger.error(f"Registration error: {e}")
            flash(f"Registration failed: {e}", "danger")
            return redirect(url_for('register'))
        
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
        
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        
        if not supabase:
            flash("Supabase not configured. Cannot log in.", "danger")
            return redirect(url_for('login'))
        
        try:
            res = supabase.table('users').select('*').eq('email', email).execute()
            
            if not res or not hasattr(res, 'data') or not res.data:
                flash("Invalid email or password.", "danger")
                return redirect(url_for('login'))
                
            user_data = res.data[0]
        except Exception as e:
            logger.error(f"Login error: {e}")
            flash(f"Login failed: {e}", "danger")
            return redirect(url_for('login'))

        try:
            if check_password_hash(user_data.get('password_hash', ''), password):
                uname = user_data.get('username', user_data.get('email', '').split('@')[0])
                user = User(user_id=user_data['id'], username=uname, email=user_data['email'])
                login_user(user)
                return redirect(url_for('dashboard'))
            else:
                flash("Invalid email or password.", "danger")
        except Exception as e:
            logger.error(f"Password check error: {e}")
            flash("Password check failed.", "danger")

    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for('home'))

@app.route('/dashboard')
@login_required
def dashboard():
    if not supabase:
        flash("Supabase not configured.", "danger")
        return render_template('dashboard.html', photos=[])
        
    photos = get_user_photos(current_user.id)
    return render_template('dashboard.html', photos=photos)

@app.route('/upload', methods=['GET', 'POST'])
@login_required
def upload():
    if request.method == 'POST':
        if 'photo' not in request.files:
            flash('No file provided.', 'danger')
            return redirect(request.url)
            
        file = request.files['photo']
        bg_color = request.form.get('bg_color', 'white')
        
        if file.filename == '':
            flash('No selected file.', 'danger')
            return redirect(request.url)
            
        ext = os.path.splitext(file.filename)[1].lower()
        if ext not in app.config['UPLOAD_EXTENSIONS']:
            flash('Invalid file extension. Please upload JPG or PNG.', 'danger')
            return redirect(request.url)
            
        try:
            input_bytes = file.read()
            processed_bytes = process_image(input_bytes, bg_color)
        except Exception as e:
            logger.exception("Error removing background / generating passport image")
            flash(f"Error processing image: {e}", 'danger')
            return redirect(request.url)

        # Try cloud save, but do not block user from getting the generated photo.
        try:
            url, blob_path = upload_to_supabase(processed_bytes, current_user.id)
            save_photo(current_user.id, url, blob_path)

            flash('Photo generated and saved to your dashboard!', 'success')
            return redirect(url_for('dashboard'))
        except Exception as e:
            logger.exception("Cloud save failed after successful processing")
            flash("Photo generated, but cloud save failed. Showing local preview.", "warning")
            image_data = base64.b64encode(processed_bytes).decode("ascii")
            return render_template(
                "result.html",
                image_data=image_data,
            )
            
    return render_template('upload.html')

@app.errorhandler(413)
def file_too_large(_e):
    flash("File too large. Max allowed size is 5MB.", "danger")
    return redirect(url_for('upload'))

@app.route('/delete/<photo_id>', methods=['POST', 'DELETE'])
@login_required
def delete_photo_route(photo_id):
    success, message = delete_photo_from_supabase(photo_id, current_user.id)
    if success:
        flash(message, "success")
    else:
        flash(message, "danger")
    return redirect(url_for('dashboard'))

if __name__ == '__main__':
    port=int(os.environ.get("PORT",10000))
    app.run(host='0.0.0.0', port=port)
