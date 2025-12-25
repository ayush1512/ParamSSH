from flask import Flask, render_template, request, jsonify, session
from flask_sock import Sock
import json
import subprocess
import os
import tempfile
import select
import pty
import struct
import fcntl
import termios
import signal
import logging

app = Flask(__name__, template_folder='templates', static_folder='static')
app.secret_key = 'your-secret-key-change-this-in-production'
sock = Sock(app)

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


@app.route('/api/slurm/nodes', methods=['GET'])
def api_slurm_nodes():
    conn = get_connection()
    if not conn:
        return jsonify({'success': False, 'message': 'Not connected'}), 401
    output, error = slurm_nodes_info(conn)
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
    app.run(debug=True, host='0.0.0.0', port=5000)