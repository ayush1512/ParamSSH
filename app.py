from flask import Flask, render_template, request, jsonify, session
from flask_sock import Sock
from werkzeug.utils import secure_filename
import json
import subprocess
import os
import tempfile
import signal
import logging
from datetime import datetime
import re
import sqlite3
import sys

# Platform-specific imports
if sys.platform != 'win32':
    import select
    import pty
    import struct
    import fcntl
    import termios

app = Flask(__name__, template_folder='templates', static_folder='static')
app.secret_key = 'your-secret-key-change-this-in-production'
sock = Sock(app)

# Database configuration
DB_PATH = 'profiles.db'

def init_db():
    """Initialize database with ssh_profiles table."""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS ssh_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            host TEXT NOT NULL,
            user TEXT NOT NULL,
            port INTEGER DEFAULT 22,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_used TIMESTAMP
        )''')
        conn.commit()
        conn.close()
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Database initialization error: {e}")

def get_db():
    """Get database connection."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# File upload settings
UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'txt', 'pdf', 'png', 'jpg', 'jpeg', 'gif', 'zip', 'gz', 'tar', 'sh', 'py', 'js', 'json', 'xml', 'csv', 'conf', 'log'}
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = MAX_FILE_SIZE

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Store connections
SSH_CONNECTIONS = {}
SHELL_PROCESSES = {}


class WebSSHConnection:
    """Non-interactive SSH connection for web apps."""
    
    def __init__(self):
        self.host = None
        self.user = None
        self.port = None
        self.connected = False
        self.control_path = None
    
    def connect(self, host, user, port):
        """Establish SSH connection."""
        try:
            logger.info(f"Connecting to {user}@{host}:{port}...")
            
            self.host = host
            self.user = user
            self.port = port
            self.control_path = os.path.join(tempfile.gettempdir(), f"ssh-web-{user}-{host}-{port}")
            
            # Test connection (assumes key-based auth or ssh-agent)
            initial_command = f"ssh -M -S {self.control_path} -o ControlPersist=10m -o StrictHostKeyChecking=no -o BatchMode=yes -p {port} {user}@{host} 'echo CONNECTION_SUCCESS'"
            result = subprocess.run(initial_command, shell=True, text=True, capture_output=True, timeout=15)
            
            if result.returncode == 0 and "CONNECTION_SUCCESS" in result.stdout:
                import time
                time.sleep(1)
                test_command = f"ssh -S {self.control_path} -O check {user}@{host} 2>&1"
                test_result = subprocess.run(test_command, shell=True, capture_output=True, text=True)
                
                if "Master running" in test_result.stdout or test_result.returncode == 0:
                    self.connected = True
                    logger.info(f"✓ Connected to {user}@{host}")
                    return True, "Connected successfully"
                else:
                    self.connected = False
                    return False, "Control socket not established"
            else:
                self.connected = False
                error_msg = result.stderr if result.stderr else "Unknown error"
                return False, f"Connection failed: {error_msg}"
                
        except subprocess.TimeoutExpired:
            return False, "Connection timeout"
        except Exception as e:
            return False, f"Error: {str(e)}"
    
    def execute_command(self, command):
        """Execute command and return output or error."""
        if not self.connected:
            return None, "Not connected"
        
        try:
            ssh_command = f"ssh -S {self.control_path} -p {self.port} {self.user}@{self.host} '{command}'"
            result = subprocess.run(
                ssh_command,
                shell=True,
                text=True,
                capture_output=True,
                timeout=60
            )
            
            if result.returncode != 0 and result.stderr:
                return result.stdout if result.stdout else None, result.stderr
            
            return result.stdout, None
            
        except subprocess.TimeoutExpired:
            return None, "Command timeout"
        except Exception as e:
            return None, str(e)
    
    def disconnect(self):
        """Cleanup connection."""
        if self.connected and self.control_path:
            try:
                subprocess.run(
                    f"ssh -S {self.control_path} -O exit {self.user}@{self.host}",
                    shell=True,
                    capture_output=True,
                    timeout=5
                )
            except:
                pass
            
            if os.path.exists(self.control_path):
                try:
                    os.remove(self.control_path)
                except:
                    pass
        
        self.connected = False


# SLURM functions
def slurm_show_jobs(ssh_conn):
    return ssh_conn.execute_command("squeue -u $USER")


def parse_slurm_jobs(ssh_conn=None):
    """Parse SLURM jobs and return structured data."""
    try:
        # If SSH connection available, use it; otherwise execute locally
        if ssh_conn:
            output, error = ssh_conn.execute_command(
                "squeue -u $USER --format='%i|%T|%j|%N|%e|%R|%C|%G|%D|%S|%e|%a' --noheader"
            )
        else:
            # Execute directly on local system
            result = subprocess.run(
                "squeue --format='%i|%T|%j|%N|%e|%R|%C|%G|%D|%S|%e|%a' --noheader",
                shell=True, capture_output=True, text=True, timeout=10
            )
            output = result.stdout
            error = result.stderr if result.returncode != 0 else None
        
        if error or not output:
            return {"success": False, "jobs": [], "error": error or "No jobs found"}
        
        jobs = []
        lines = output.strip().split('\n')
        
        for line in lines:
            if not line.strip():
                continue
                
            parts = line.split('|')
            if len(parts) < 8:
                continue
            
            job_id = parts[0].strip()
            status = parts[1].strip()
            job_name = parts[2].strip()
            nodes = parts[3].strip()
            end_time = parts[4].strip()
            reason = parts[5].strip()
            cpus = parts[6].strip()
            gpus = parts[7].strip() if len(parts) > 7 else "0"
            num_nodes = parts[8].strip() if len(parts) > 8 else "1"
            
            # Get job start time with scontrol for elapsed time calculation
            if ssh_conn:
                start_output, _ = ssh_conn.execute_command(f"scontrol show job {job_id} | grep StartTime")
            else:
                result = subprocess.run(
                    f"scontrol show job {job_id} | grep StartTime",
                    shell=True, capture_output=True, text=True, timeout=10
                )
                start_output = result.stdout
            start_time = ""
            elapsed_time = ""
            
            if start_output:
                match = re.search(r'StartTime=([^\s]+)', start_output)
                if match:
                    start_time = match.group(1)
                    if start_time != "None":
                        try:
                            start_dt = datetime.strptime(start_time, "%Y-%m-%dT%H:%M:%S")
                            elapsed = datetime.now() - start_dt
                            hours = elapsed.seconds // 3600
                            minutes = (elapsed.seconds % 3600) // 60
                            elapsed_time = f"{hours}h {minutes}m"
                        except:
                            elapsed_time = "Calculating..."
            
            # Map status to badges
            status_color = {
                'RUNNING': 'success',
                'PENDING': 'warning',
                'COMPLETED': 'info',
                'FAILED': 'error',
                'CANCELLED': 'muted',
                'STOPPED': 'muted',
                'TIMEOUT': 'error'
            }.get(status, 'muted')
            
            jobs.append({
                'job_id': job_id,
                'name': job_name,
                'status': status,
                'status_color': status_color,
                'nodes': nodes if nodes != "(None)" else "N/A",
                'num_nodes': num_nodes,
                'cpus': cpus if cpus else "0",
                'gpus': gpus if gpus and gpus != "0" else "0",
                'reason': reason if reason != "(None)" else "",
                'elapsed_time': elapsed_time,
                'start_time': start_time if start_time != "None" else "Not started"
            })
        
        return {"success": True, "jobs": jobs, "total": len(jobs)}
        
    except Exception as e:
        logger.error(f"Error parsing SLURM jobs: {e}")
        return {"success": False, "jobs": [], "error": str(e)}


def get_slurm_gpu_info(ssh_conn=None):
    """Get GPU resource information from SLURM."""
    try:
        # Get GPU info from sinfo
        if ssh_conn:
            output, error = ssh_conn.execute_command(
                "sinfo -o '%N|%g|%t' --noheader 2>/dev/null || echo 'No GPU info available'"
            )
        else:
            result = subprocess.run(
                "sinfo -o '%N|%g|%t' --noheader 2>/dev/null || echo 'No GPU info available'",
                shell=True, capture_output=True, text=True, timeout=10
            )
            output = result.stdout
            error = result.stderr if result.returncode != 0 else None
        
        gpu_info = []
        if output and "No GPU" not in output:
            for line in output.strip().split('\n'):
                if line.strip():
                    parts = line.split('|')
                    if len(parts) >= 3:
                        gpu_info.append({
                            'node': parts[0].strip(),
                            'gpus': parts[1].strip() if parts[1].strip() else "0",
                            'state': parts[2].strip()
                        })
        
        return {"success": True, "gpu_info": gpu_info}
    except Exception as e:
        logger.error(f"Error getting GPU info: {e}")
        return {"success": False, "gpu_info": [], "error": str(e)}


def get_slurm_nodes_info(ssh_conn=None):
    """Get detailed node information from SLURM."""
    try:
        if ssh_conn:
            output, error = ssh_conn.execute_command(
                "sinfo -o '%N|%t|%c|%m|%g' --noheader"
            )
        else:
            result = subprocess.run(
                "sinfo -o '%N|%t|%c|%m|%g' --noheader",
                shell=True, capture_output=True, text=True, timeout=10
            )
            output = result.stdout
            error = result.stderr if result.returncode != 0 else None
        
        if error or not output:
            return {"success": False, "nodes": []}
        
        nodes = []
        for line in output.strip().split('\n'):
            if not line.strip():
                continue
            
            parts = line.split('|')
            if len(parts) < 5:
                continue
            
            node_name = parts[0].strip()
            state = parts[1].strip()
            cpus = parts[2].strip()
            memory = parts[3].strip()
            gpus = parts[4].strip() if parts[4].strip() else "0"
            
            # Determine state color
            state_color = 'success' if state == 'idle' else ('warning' if state in ['alloc', 'mixed'] else 'muted')
            
            nodes.append({
                'name': node_name,
                'state': state,
                'state_color': state_color,
                'cpus': cpus,
                'memory': memory,
                'gpus': gpus
            })
        
        return {"success": True, "nodes": nodes, "total": len(nodes)}
    except Exception as e:
        logger.error(f"Error getting nodes info: {e}")
        return {"success": False, "nodes": []}


def slurm_submit_job(ssh_conn, script_path):
    return ssh_conn.execute_command(f"sbatch {script_path}")

def slurm_cancel_job(ssh_conn, job_id):
    return ssh_conn.execute_command(f"scancel {job_id}")

def slurm_job_info(ssh_conn, job_id):
    return ssh_conn.execute_command(f"scontrol show job {job_id}")

def slurm_nodes_info(ssh_conn):
    return ssh_conn.execute_command("sinfo")


# Conda functions
def conda_list_envs(ssh_conn):
    return ssh_conn.execute_command("conda env list")

def conda_create_env(ssh_conn, env_name, python_version=None):
    if python_version:
        cmd = f"conda create -n {env_name} python={python_version} -y"
    else:
        cmd = f"conda create -n {env_name} -y"
    return ssh_conn.execute_command(cmd)

def conda_remove_env(ssh_conn, env_name):
    return ssh_conn.execute_command(f"conda env remove -n {env_name} -y")

def conda_install_package(ssh_conn, env_name, package):
    return ssh_conn.execute_command(f"conda install -n {env_name} {package} -y")


# System info
def get_remote_system_info(ssh_conn):
    return ssh_conn.execute_command("uname -a && echo '---' && df -h && echo '---' && free -h")


# Routes
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/connect', methods=['POST'])
def api_connect():
    data = request.json
    host = data.get('host')
    user = data.get('user')
    port = data.get('port', 22)
    
    if not all([host, user]):
        return jsonify({'success': False, 'message': 'Missing host or user'}), 400
    
    conn = WebSSHConnection()
    success, message = conn.connect(host, user, str(port))
    
    if success:
        session_id = f"{user}@{host}:{port}"
        SSH_CONNECTIONS[session_id] = conn
        session['ssh_session'] = session_id
        return jsonify({'success': True, 'message': message, 'session_id': session_id})
    else:
        return jsonify({'success': False, 'message': message}), 400


@app.route('/api/disconnect', methods=['POST'])
def api_disconnect():
    session_id = session.get('ssh_session')
    if session_id and session_id in SSH_CONNECTIONS:
        SSH_CONNECTIONS[session_id].disconnect()
        del SSH_CONNECTIONS[session_id]
        session.pop('ssh_session', None)
        return jsonify({'success': True, 'message': 'Disconnected'})
    return jsonify({'success': False, 'message': 'No active session'}), 400


def get_connection():
    """Helper to get current SSH connection."""
    session_id = session.get('ssh_session')
    if not session_id or session_id not in SSH_CONNECTIONS:
        return None
    return SSH_CONNECTIONS[session_id]


@app.route('/api/slurm/jobs', methods=['GET'])
def api_slurm_jobs():
    conn = get_connection()
    if not conn:
        return jsonify({'success': False, 'message': 'Not connected'}), 401
    output, error = slurm_show_jobs(conn)
    if error:
        return jsonify({'success': False, 'error': error}), 400
    return jsonify({'success': True, 'data': output})


@app.route('/api/slurm/dashboard', methods=['GET'])
def api_slurm_dashboard():
    """Get parsed SLURM jobs data for dashboard."""
    try:
        # Try to use SSH connection if available, otherwise use local commands
        conn = get_connection()
        
        jobs_data = parse_slurm_jobs(conn)
        nodes_data = get_slurm_nodes_info(conn)
        gpu_data = get_slurm_gpu_info(conn)
        
        return jsonify({
            'success': True,
            'jobs': jobs_data.get('jobs', []),
            'total_jobs': jobs_data.get('total', 0),
            'nodes': nodes_data.get('nodes', []),
            'total_nodes': nodes_data.get('total', 0),
            'gpu_info': gpu_data.get('gpu_info', []),
            'timestamp': datetime.now().isoformat()
        }), 200
    except Exception as e:
        logger.error(f"Dashboard error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/slurm/nodes', methods=['GET'])
def api_slurm_nodes():
    """Get SLURM nodes information."""
    try:
        conn = get_connection()
        nodes_data = get_slurm_nodes_info(conn)
        return jsonify(nodes_data), 200
    except Exception as e:
        logger.error(f"Nodes info error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/slurm/gpu-info', methods=['GET'])
def api_slurm_gpu_info():
    """Get SLURM GPU information."""
    try:
        conn = get_connection()
        gpu_data = get_slurm_gpu_info(conn)
        return jsonify(gpu_data), 200
    except Exception as e:
        logger.error(f"GPU info error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/slurm/submit', methods=['POST'])
def api_slurm_submit():
    conn = get_connection()
    if not conn:
        return jsonify({'success': False, 'message': 'Not connected'}), 401
    data = request.json
    script_path = data.get('script_path')
    if not script_path:
        return jsonify({'success': False, 'message': 'Missing script path'}), 400
    output, error = slurm_submit_job(conn, script_path)
    if error:
        return jsonify({'success': False, 'error': error}), 400
    return jsonify({'success': True, 'data': output})


@app.route('/api/slurm/cancel', methods=['POST'])
def api_slurm_cancel():
    conn = get_connection()
    if not conn:
        return jsonify({'success': False, 'message': 'Not connected'}), 401
    data = request.json
    job_id = data.get('job_id')
    if not job_id:
        return jsonify({'success': False, 'message': 'Missing job ID'}), 400
    output, error = slurm_cancel_job(conn, job_id)
    if error:
        return jsonify({'success': False, 'error': error}), 400
    return jsonify({'success': True, 'data': output or 'Job cancelled'})


@app.route('/api/slurm/info', methods=['GET'])
def api_slurm_info():
    conn = get_connection()
    if not conn:
        return jsonify({'success': False, 'message': 'Not connected'}), 401
    job_id = request.args.get('job_id')
    if not job_id:
        return jsonify({'success': False, 'message': 'Missing job ID'}), 400
    output, error = slurm_job_info(conn, job_id)
    if error:
        return jsonify({'success': False, 'error': error}), 400
    return jsonify({'success': True, 'data': output})


@app.route('/api/conda/envs', methods=['GET'])
def api_conda_envs():
    conn = get_connection()
    if not conn:
        return jsonify({'success': False, 'message': 'Not connected'}), 401
    output, error = conda_list_envs(conn)
    if error:
        return jsonify({'success': False, 'error': error}), 400
    return jsonify({'success': True, 'data': output})


@app.route('/api/conda/create', methods=['POST'])
def api_conda_create():
    conn = get_connection()
    if not conn:
        return jsonify({'success': False, 'message': 'Not connected'}), 401
    data = request.json
    env_name = data.get('env_name')
    python_version = data.get('python_version')
    if not env_name:
        return jsonify({'success': False, 'message': 'Missing env name'}), 400
    output, error = conda_create_env(conn, env_name, python_version)
    if error:
        return jsonify({'success': False, 'error': error}), 400
    return jsonify({'success': True, 'data': output})


@app.route('/api/conda/remove', methods=['POST'])
def api_conda_remove():
    conn = get_connection()
    if not conn:
        return jsonify({'success': False, 'message': 'Not connected'}), 401
    data = request.json
    env_name = data.get('env_name')
    if not env_name:
        return jsonify({'success': False, 'message': 'Missing env name'}), 400
    output, error = conda_remove_env(conn, env_name)
    if error:
        return jsonify({'success': False, 'error': error}), 400
    return jsonify({'success': True, 'data': output})


@app.route('/api/conda/install', methods=['POST'])
def api_conda_install():
    conn = get_connection()
    if not conn:
        return jsonify({'success': False, 'message': 'Not connected'}), 401
    data = request.json
    env_name = data.get('env_name')
    package = data.get('package')
    if not env_name or not package:
        return jsonify({'success': False, 'message': 'Missing env name or package'}), 400
    output, error = conda_install_package(conn, env_name, package)
    if error:
        return jsonify({'success': False, 'error': error}), 400
    return jsonify({'success': True, 'data': output})


@app.route('/api/system/info', methods=['GET'])
def api_system_info():
    conn = get_connection()
    if not conn:
        return jsonify({'success': False, 'message': 'Not connected'}), 401
    output, error = get_remote_system_info(conn)
    if error:
        return jsonify({'success': False, 'error': error}), 400
    return jsonify({'success': True, 'data': output})


def allowed_file(filename):
    """Check if file extension is allowed."""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


@app.route('/api/upload', methods=['POST'])
def upload_file():
    """Handle file upload."""
    try:
        if 'file' not in request.files:
            return jsonify({'success': False, 'message': 'No file provided'}), 400
        
        file = request.files['file']
        if file.filename == '':
            return jsonify({'success': False, 'message': 'No file selected'}), 400
        
        if not allowed_file(file.filename):
            return jsonify({'success': False, 'message': 'File type not allowed'}), 400
        
        filename = secure_filename(file.filename)
        # Add timestamp to prevent name collisions
        import time
        filename = f"{int(time.time())}_{filename}"
        
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)
        
        logger.info(f"File uploaded: {filename}")
        return jsonify({
            'success': True,
            'message': 'File uploaded successfully',
            'filename': filename,
            'path': filepath
        }), 200
        
    except Exception as e:
        logger.error(f"Upload error: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/files', methods=['GET'])
def get_files():
    """Get list of uploaded files."""
    try:
        files = []
        if os.path.exists(app.config['UPLOAD_FOLDER']):
            for filename in os.listdir(app.config['UPLOAD_FOLDER']):
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                if os.path.isfile(filepath):
                    size = os.path.getsize(filepath)
                    mtime = os.path.getmtime(filepath)
                    files.append({
                        'name': filename,
                        'size': size,
                        'modified': mtime,
                        'url': f'/uploads/{filename}'
                    })
        
        # Sort by modification time (newest first)
        files.sort(key=lambda x: x['modified'], reverse=True)
        return jsonify({'success': True, 'files': files}), 200
        
    except Exception as e:
        logger.error(f"Error listing files: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/files/<filename>', methods=['DELETE'])
def delete_file(filename):
    """Delete an uploaded file."""
    try:
        filename = secure_filename(filename)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        
        if not os.path.exists(filepath):
            return jsonify({'success': False, 'message': 'File not found'}), 404
        
        os.remove(filepath)
        logger.info(f"File deleted: {filename}")
        return jsonify({'success': True, 'message': 'File deleted'}), 200
        
    except Exception as e:
        logger.error(f"Delete error: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500


# SSH Profiles Management API
@app.route('/api/profiles/save', methods=['POST'])
def save_profile():
    """Save a new SSH profile."""
    try:
        data = request.json
        name = data.get('name', '').strip()
        host = data.get('host', '').strip()
        user = data.get('user', '').strip()
        port = data.get('port', 22)
        
        if not all([name, host, user]):
            return jsonify({'success': False, 'message': 'Missing required fields (name, host, user)'}), 400
        
        if not isinstance(port, int) or port < 1 or port > 65535:
            return jsonify({'success': False, 'message': 'Invalid port number'}), 400
        
        try:
            db = get_db()
            db.execute(
                "INSERT INTO ssh_profiles (name, host, user, port) VALUES (?, ?, ?, ?)",
                (name, host, user, port)
            )
            db.commit()
            db.close()
            logger.info(f"SSH profile saved: {name}")
            return jsonify({'success': True, 'message': f'Profile "{name}" saved'}), 200
        except sqlite3.IntegrityError:
            return jsonify({'success': False, 'message': f'Profile "{name}" already exists'}), 400
    
    except Exception as e:
        logger.error(f"Save profile error: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/profiles', methods=['GET'])
def list_profiles():
    """Get all saved SSH profiles."""
    try:
        db = get_db()
        cur = db.execute("SELECT id, name, host, user, port, created_at, last_used FROM ssh_profiles ORDER BY created_at DESC")
        profiles = [dict(row) for row in cur.fetchall()]
        db.close()
        
        return jsonify({'success': True, 'profiles': profiles}), 200
    except Exception as e:
        logger.error(f"List profiles error: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/profiles/<int:profile_id>', methods=['DELETE'])
def delete_profile(profile_id):
    """Delete a saved SSH profile."""
    try:
        db = get_db()
        cur = db.execute("SELECT name FROM ssh_profiles WHERE id=?", (profile_id,))
        profile = cur.fetchone()
        
        if not profile:
            return jsonify({'success': False, 'message': 'Profile not found'}), 404
        
        db.execute("DELETE FROM ssh_profiles WHERE id=?", (profile_id,))
        db.commit()
        db.close()
        
        logger.info(f"SSH profile deleted: {profile['name']}")
        return jsonify({'success': True, 'message': f'Profile "{profile["name"]}" deleted'}), 200
    except Exception as e:
        logger.error(f"Delete profile error: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/profiles/<int:profile_id>', methods=['PUT'])
def update_profile(profile_id):
    """Update a saved SSH profile."""
    try:
        data = request.json
        name = data.get('name', '').strip()
        host = data.get('host', '').strip()
        user = data.get('user', '').strip()
        port = data.get('port', 22)
        
        if not all([name, host, user]):
            return jsonify({'success': False, 'message': 'Missing required fields'}), 400
        
        if not isinstance(port, int) or port < 1 or port > 65535:
            return jsonify({'success': False, 'message': 'Invalid port number'}), 400
        
        db = get_db()
        cur = db.execute("SELECT id FROM ssh_profiles WHERE id=?", (profile_id,))
        if not cur.fetchone():
            return jsonify({'success': False, 'message': 'Profile not found'}), 404
        
        try:
            db.execute(
                "UPDATE ssh_profiles SET name=?, host=?, user=?, port=? WHERE id=?",
                (name, host, user, port, profile_id)
            )
            db.commit()
            db.close()
            logger.info(f"SSH profile updated: {name}")
            return jsonify({'success': True, 'message': f'Profile "{name}" updated'}), 200
        except sqlite3.IntegrityError:
            return jsonify({'success': False, 'message': f'Profile name "{name}" already exists'}), 400
    
    except Exception as e:
        logger.error(f"Update profile error: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/profiles/<int:profile_id>/connect', methods=['POST'])
def connect_from_profile(profile_id):
    """Connect using a saved profile."""
    try:
        db = get_db()
        cur = db.execute("SELECT host, user, port FROM ssh_profiles WHERE id=?", (profile_id,))
        profile = cur.fetchone()
        
        if not profile:
            return jsonify({'success': False, 'message': 'Profile not found'}), 404
        
        # Update last_used timestamp
        db.execute("UPDATE ssh_profiles SET last_used=CURRENT_TIMESTAMP WHERE id=?", (profile_id,))
        db.commit()
        db.close()
        
        # Use the existing connect endpoint logic with profile data
        return jsonify({
            'success': True,
            'host': profile['host'],
            'user': profile['user'],
            'port': profile['port']
        }), 200
    
    except Exception as e:
        logger.error(f"Profile connect error: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500


# WebSocket for interactive terminal
@sock.route('/ws/terminal')
def terminal_ws(ws):
    """WebSocket handler for interactive terminal."""
    fd = None
    child_pid = None
    
    try:
        while True:
            data = ws.receive()
            if data is None:
                break
            
            msg = json.loads(data)
            
            if msg['type'] == 'connect':
                host = msg['host']
                user = msg['user']
                port = msg['port']
                
                # Fork a PTY for interactive SSH
                child_pid, fd = pty.fork()
                
                if child_pid == 0:
                    # Child process - exec ssh
                    os.execlp('ssh', 'ssh', 
                              '-o', 'StrictHostKeyChecking=no',
                              '-p', str(port),
                              f'{user}@{host}')
                else:
                    # Parent process
                    # Set non-blocking
                    import fcntl
                    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
                    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
                    
                    ws.send(json.dumps({'type': 'connected'}))
                    
                    # Start reading from PTY
                    import threading
                    
                    def read_output():
                        try:
                            while True:
                                try:
                                    output = os.read(fd, 1024)
                                    if output:
                                        ws.send(json.dumps({
                                            'type': 'output',
                                            'data': output.decode('utf-8', errors='replace')
                                        }))
                                except BlockingIOError:
                                    import time
                                    time.sleep(0.01)
                                except OSError:
                                    break
                        except Exception as e:
                            logger.error(f"Read error: {e}")
                    
                    read_thread = threading.Thread(target=read_output, daemon=True)
                    read_thread.start()
            
            elif msg['type'] == 'input' and fd:
                os.write(fd, msg['data'].encode('utf-8'))
            
            elif msg['type'] == 'resize' and fd:
                # Resize PTY
                winsize = struct.pack('HHHH', msg['rows'], msg['cols'], 0, 0)
                fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
    
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        ws.send(json.dumps({'type': 'error', 'data': str(e)}))
    
    finally:
        if fd:
            os.close(fd)
        if child_pid:
            try:
                os.kill(child_pid, signal.SIGTERM)
            except:
                pass


if __name__ == '__main__':
    # Initialize database
    init_db()
    
    app.run(debug=True, host='0.0.0.0', port=5000)