import os
from flask import Flask, render_template, request, jsonify, redirect, url_for, send_from_directory, session
from flask_sqlalchemy import SQLAlchemy
from werkzeug.utils import secure_filename
from datetime import datetime
import requests
import secrets
import boto3
from botocore.client import Config
from functools import wraps

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', secrets.token_hex(16))

# Database URL handling
# Railway can use different variable names depending on how the database was created
database_url = (
        os.environ.get('DATABASE_URL') or
        os.environ.get('DATABASE_PRIVATE_URL') or
        os.environ.get('DATABASE_PUBLIC_URL') or
        os.environ.get('POSTGRES_URL') or
        os.environ.get('POSTGRESQL_URL')
)

if not database_url:
    # No DATABASE_URL set - use SQLite for local development
    database_url = 'sqlite:///babyshower.db'
    print("WARNING: No DATABASE_URL set, using SQLite for local development")
else:
    # Railway PostgreSQL compatibility: convert postgres:// to postgresql://
    if database_url.startswith('postgres://'):
        database_url = database_url.replace('postgres://', 'postgresql://', 1)
        print(f"Converted DATABASE_URL from postgres:// to postgresql://")

    # Force use of psycopg (version 3) driver instead of psycopg2
    # This is required for Python 3.13 compatibility
    if database_url.startswith('postgresql://') and '+psycopg' not in database_url:
        database_url = database_url.replace('postgresql://', 'postgresql+psycopg://', 1)
        print(f"Using psycopg3 driver for PostgreSQL")

app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_size': 10,
    'pool_recycle': 3600,
    'pool_pre_ping': True,
    'max_overflow': 20
}
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size
app.config['ADMIN_PASSWORD'] = os.environ.get('ADMIN_PASSWORD')
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('FLASK_ENV') == 'production'
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# R2 Configuration
app.config['R2_ACCOUNT_ID'] = os.environ.get('R2_ACCOUNT_ID')
app.config['R2_ACCESS_KEY_ID'] = os.environ.get('R2_ACCESS_KEY_ID')
app.config['R2_SECRET_ACCESS_KEY'] = os.environ.get('R2_SECRET_ACCESS_KEY')
app.config['R2_BUCKET_NAME'] = os.environ.get('R2_BUCKET_NAME')

db = SQLAlchemy(app)

# Admin push notifications
def send_pushover_notification(caption):
    """Send push notification via Pushover when photo uploaded"""
    if not all([app.config.get('PUSHOVER_APP_TOKEN'), app.config.get('PUSHOVER_USER_KEY')]):
        print("Pushover not configured, skipping notification")
        return

    try:
        response = requests.post('https://api.pushover.net/1/messages.json', data={
            'token': os.environ.get('PUSHOVER_APP_TOKEN'),
            'user': os.environ.get('PUSHOVER_USER_KEY'),
            'message': f'Caption: "{caption[:100]}"',
            'title': '📸 New Photo Uploaded',
            'url': 'https://andreassi-baby-shower.katahdinlogic.com/admin',
            'url_title': 'Open Admin Dashboard',
            'priority': 0,  # Normal priority
            'sound': 'pushover'  # Default sound
        })

        if response.status_code == 200:
            print(f"Pushover notification sent for photo: {caption[:30]}...")
        else:
            print(f"Pushover notification failed: {response.text}")

    except Exception as e:
        print(f"Failed to send Pushover notification: {e}")
        # Don't fail the upload if notification fails

# Admin authentication decorator
def admin_required(f):
    """Decorator to require admin authentication for routes"""

    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('admin_authenticated'):
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)

    return decorated_function


def check_admin_password(password):
    """Verify admin password"""
    return password == app.config['ADMIN_PASSWORD']


# Initialize R2 client
def get_r2_client():
    """Create and return an R2 client using boto3"""
    if not all([app.config['R2_ACCOUNT_ID'], app.config['R2_ACCESS_KEY_ID'], app.config['R2_SECRET_ACCESS_KEY']]):
        raise ValueError(
            "R2 credentials not configured. Please set R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, and R2_SECRET_ACCESS_KEY environment variables.")

    endpoint = f"https://{app.config['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com"
    print(f"Connecting to R2 endpoint: {endpoint}")

    try:
        client = boto3.client(
            's3',
            endpoint_url=endpoint,
            aws_access_key_id=app.config['R2_ACCESS_KEY_ID'],
            aws_secret_access_key=app.config['R2_SECRET_ACCESS_KEY'],
            config=Config(
                signature_version='s3v4',
                s3={'addressing_style': 'path'}
            ),
            region_name='auto',
            verify=True  # Ensure SSL verification is enabled
        )
        print("R2 client created successfully")
        return client
    except Exception as e:
        print(f"Error creating R2 client: {type(e).__name__}: {e}")
        raise


# Database Model
class Photo(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255), nullable=False)
    caption = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), default='pending')  # pending, approved, rejected
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)
    approved_at = db.Column(db.DateTime, nullable=True)

    def to_dict(self):
        return {
            'id': self.id,
            'filename': self.filename,
            'caption': self.caption,
            'status': self.status,
            'uploaded_at': self.uploaded_at.isoformat(),
            'approved_at': self.approved_at.isoformat() if self.approved_at else None
        }


# Create tables
with app.app_context():
    db.create_all()


# Routes
@app.route('/')
def index():
    return redirect(url_for('upload'))


@app.route('/upload', methods=['GET', 'POST'])
def upload():
    if request.method == 'POST':
        # Check file size before processing
        if request.content_length and request.content_length > app.config['MAX_CONTENT_LENGTH']:
            return jsonify({'error': 'File too large. Maximum size is 16MB.'}), 413

        if 'photo' not in request.files:
            return jsonify({'error': 'No photo uploaded'}), 400

        photo = request.files['photo']
        caption = request.form.get('caption', '').strip()

        if not caption:
            return jsonify({'error': 'Caption is required'}), 400

        if photo.filename == '':
            return jsonify({'error': 'No photo selected'}), 400

        if photo and allowed_file(photo.filename):
            # Generate unique filename
            filename = secure_filename(f"{datetime.utcnow().timestamp()}_{photo.filename}")

            try:
                # Upload to R2
                r2_client = get_r2_client()

                # Reset file pointer to beginning (important!)
                photo.seek(0)

                r2_client.upload_fileobj(
                    photo,
                    app.config['R2_BUCKET_NAME'],
                    filename,
                    ExtraArgs={
                        'ContentType': photo.content_type
                    }
                )

                # Only commit to database if R2 upload succeeded
                try:
                    new_photo = Photo(filename=filename, caption=caption)
                    db.session.add(new_photo)
                    db.session.commit()

                    print("DEBUG: Database commit successful")
                    print("DEBUG: About to send Pushover notification")

                    # Send push notification
                    send_pushover_notification(caption)

                    print("DEBUG: Pushover notification function completed")

                    return jsonify({'success': True, 'message': 'Photo uploaded!'})

                except Exception as db_error:
                    # Database save failed - delete from R2 to keep consistent
                    print(f"Database save failed, cleaning up R2: {db_error}")
                    try:
                        r2_client.delete_object(
                            Bucket=app.config['R2_BUCKET_NAME'],
                            Key=filename
                        )
                        print(f"Cleaned up orphaned file from R2: {filename}")
                    except Exception as cleanup_error:
                        print(f"Failed to cleanup R2 file: {cleanup_error}")

                    return jsonify({'error': 'Database error. Please try again.'}), 500

            except ValueError as e:
                # R2 credentials not configured
                print(f"R2 configuration error: {e}")
                return jsonify({'error': 'Server configuration error. Please contact admin.'}), 500

            except Exception as e:
                # Other upload errors
                print(f"Error uploading to R2: {type(e).__name__}: {e}")
                import traceback
                traceback.print_exc()
                return jsonify({'error': f'Upload failed: {str(e)}'}), 500

        return jsonify({'error': 'Invalid file type'}), 400

    return render_template('upload.html')


@app.route('/admin', methods=['GET', 'POST'])
def admin():
    if request.method == 'POST':
        password = request.form.get('password')
        if check_admin_password(password):
            session['admin_authenticated'] = True
            session.permanent = False  # Session expires when browser closes
            return redirect(url_for('admin_dashboard'))
        return render_template('admin_login.html', error='Invalid password')

    # If already authenticated, redirect to dashboard
    if session.get('admin_authenticated'):
        return redirect(url_for('admin_dashboard'))

    return render_template('admin_login.html')


@app.route('/admin/dashboard')
@admin_required
def admin_dashboard():
    return render_template('admin.html')


@app.route('/admin/logout', methods=['POST'])
def admin_logout():
    session.pop('admin_authenticated', None)
    return redirect(url_for('admin'))


@app.route('/display')
def display():
    return render_template('display.html')


# API Routes
@app.route('/api/photos')
def get_photos():
    status = request.args.get('status', 'approved')
    photos = Photo.query.filter_by(status=status).order_by(Photo.approved_at.desc()).all()
    return jsonify([photo.to_dict() for photo in photos])


@app.route('/api/photos/<int:photo_id>/approve', methods=['POST'])
@admin_required
def approve_photo(photo_id):
    photo = Photo.query.get_or_404(photo_id)
    photo.status = 'approved'
    photo.approved_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'success': True})


@app.route('/api/photos/<int:photo_id>/reject', methods=['POST'])
@admin_required
def reject_photo(photo_id):
    photo = Photo.query.get_or_404(photo_id)
    photo.status = 'rejected'
    db.session.commit()
    return jsonify({'success': True})


@app.route('/api/photos/<int:photo_id>/unapprove', methods=['POST'])
@admin_required
def unapprove_photo(photo_id):
    photo = Photo.query.get_or_404(photo_id)
    photo.status = 'pending'
    photo.approved_at = None
    db.session.commit()
    return jsonify({'success': True})


@app.route('/api/photos/<int:photo_id>/to-pending', methods=['POST'])
@admin_required
def to_pending(photo_id):
    """Move photo to pending from any status"""
    photo = Photo.query.get_or_404(photo_id)
    photo.status = 'pending'
    photo.approved_at = None
    db.session.commit()
    return jsonify({'success': True})


@app.route('/api/photos/<int:photo_id>/delete', methods=['POST'])
@admin_required
def delete_photo(photo_id):
    photo = Photo.query.get_or_404(photo_id)

    try:
        # Delete from R2
        r2_client = get_r2_client()
        r2_client.delete_object(
            Bucket=app.config['R2_BUCKET_NAME'],
            Key=photo.filename
        )
    except Exception as e:
        print(f"Error deleting from R2: {e}")
        # Continue with database deletion even if R2 deletion fails

    # Delete from database
    db.session.delete(photo)
    db.session.commit()
    return jsonify({'success': True})


@app.route('/api/heartbeat')
def heartbeat():
    """Simple endpoint to verify server is alive"""
    return jsonify({'status': 'ok', 'timestamp': datetime.utcnow().isoformat()})


@app.route('/uploads/<filename>')
def uploaded_file(filename):
    """Serve photo from R2 with presigned URL"""
    try:
        r2_client = get_r2_client()

        # Generate presigned URL (valid for 1 hour)
        url = r2_client.generate_presigned_url(
            'get_object',
            Params={
                'Bucket': app.config['R2_BUCKET_NAME'],
                'Key': filename
            },
            ExpiresIn=3600  # 1 hour
        )

        return redirect(url)

    except Exception as e:
        print(f"Error generating presigned URL: {e}")
        return "File not found", 404


def allowed_file(filename):
    ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'heic'}
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5003)