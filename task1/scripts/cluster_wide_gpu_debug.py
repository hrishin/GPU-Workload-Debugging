#!/usr/bin/env python3
"""
Cluster-wide GPU Configuration Analyzer

Runs GPU failure detection on all nodes in the cluster simultaneously
and collects containerd configurations from each node.
"""

import subprocess
import json
import yaml
import threading
import time
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple
from pathlib import Path
import tempfile
import base64

@dataclass
class NodeInfo:
    name: str
    roles: List[str]
    status: str
    gpu_pods: int
    has_gpu_resources: bool

@dataclass
class ContainerdConfig:
    node_name: str
    config_path: str
    exists: bool
    nvidia_runtime_configured: bool
    config_content: str
    binary_name: str = ""
    binary_exists: bool = False
    binary_path_used: str = ""
    error: Optional[str] = None

@dataclass
class NodeGPUStatus:
    node_name: str
    containerd_configs: List[ContainerdConfig]
    gpu_failure_symptoms: Optional[str] = None
    debug_pod_deployed: bool = False
    execution_error: Optional[str] = None

class ClusterGPUAnalyzer:
    def __init__(self, namespace: str = "kube-system", max_workers: int = 5):
        self.namespace = namespace
        self.max_workers = max_workers
        self.debug_pod_prefix = "gpu-debug"
        self.kubectl_available = self._check_kubectl()
        
    def _check_kubectl(self) -> bool:
        """Check if kubectl is available and configured"""
        try:
            subprocess.run(['kubectl', 'version', '--client'], 
                         capture_output=True, check=True)
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False
    
    def _run_kubectl(self, args: list) -> tuple:
        """Run kubectl command and return success status and output"""
        if not self.kubectl_available:
            return False, "kubectl not available"
        
        try:
            cmd = ['kubectl'] + args
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            return True, result.stdout
        except subprocess.CalledProcessError as e:
            return False, e.stderr or str(e)
        
    def get_pending_gpu_pods(self) -> list:
        """Get all pending GPU pods in the cluster"""
        pending_pods = []
        
        success, output = self._run_kubectl([
            'get', 'pods', '--all-namespaces', '-o', 'json'
        ])
        
        if not success:
            return pending_pods
        
        try:
            pods_data = json.loads(output)
            for pod in pods_data.get('items', []):
                # Check if pod requests GPU resources
                has_gpu_request = False
                containers = pod.get('spec', {}).get('containers', [])
                
                for container in containers:
                    resources = container.get('resources', {})
                    requests = resources.get('requests', {})
                    if 'nvidia.com/gpu' in requests:
                        has_gpu_request = True
                        break
                
                if has_gpu_request:
                    status = pod.get('status', {})
                    phase = status.get('phase', 'Unknown')
                    
                    # Check for pending or error states
                    container_statuses = status.get('containerStatuses', [])
                    has_errors = any(
                        cs.get('state', {}).get('waiting', {}).get('reason', '') 
                        in ['CreateContainerError', 'ContainerStatusUnknown']
                        for cs in container_statuses
                    )
                    
                    if phase == 'Pending' or has_errors:
                        pending_pods.append({
                            'name': pod['metadata']['name'],
                            'namespace': pod['metadata']['namespace'],
                            'status': phase,
                            'node': pod.get('spec', {}).get('nodeName', 'Not scheduled')
                        })
        
        except json.JSONDecodeError:
            pass
        
        return pending_pods
    
    def get_containerd_runtime_errors(self) -> list:
        """Get containerd runtime errors from all nodes"""
        # This will be collected from GPU detection on each node
        # For now, return empty - will be populated per-node
        return []
    
    def get_runtime_config_issues(self, results: dict) -> list:
        """Identify runtime configuration issues from node results"""
        issues = []
        
        for node_name, result in results.items():
            if result.execution_error:
                continue
            
            # Check if any config has NVIDIA runtime
            has_nvidia_runtime = any(
                c.nvidia_runtime_configured 
                for c in result.containerd_configs 
                if c.exists
            )
            
            if not has_nvidia_runtime:
                issues.append(f"Node {node_name}: NVIDIA runtime not configured in containerd config")
            
            # Check for missing binaries
            for config in result.containerd_configs:
                if config.nvidia_runtime_configured and config.binary_name and not config.binary_exists:
                    issues.append(f"Node {node_name}: NVIDIA runtime binary not found at {config.binary_name}")
        
        return issues
    
    def check_nvidia_device_plugin_status(self) -> dict:
        """Check NVIDIA device plugin daemonset status"""
        status = {}
        
        success, output = self._run_kubectl([
            'get', 'daemonset', 'nvidia-device-plugin-daemonset', 
            '-n', 'gpu-operator', '-o', 'json'
        ])
        
        if success:
            try:
                ds_data = json.loads(output)
                status_info = ds_data.get('status', {})
                status['desired'] = str(status_info.get('desiredNumberScheduled', 0))
                status['ready'] = str(status_info.get('numberReady', 0))
                status['available'] = str(status_info.get('numberAvailable', 0))
            except json.JSONDecodeError:
                status['error'] = 'Failed to parse device plugin status'
        else:
            status['error'] = f'Device plugin not found'
        
        return status
    
    def check_nvidia_container_toolkit_status(self) -> str:
        """Check NVIDIA Container Toolkit daemonset status"""
        success, output = self._run_kubectl([
            'get', 'daemonset', 'nvidia-container-toolkit-daemonset', 
            '-n', 'gpu-operator', '-o', 'json'
        ])
        
        if success:
            try:
                ds_data = json.loads(output)
                status_info = ds_data.get('status', {})
                ready = status_info.get('numberReady', 0)
                desired = status_info.get('desiredNumberScheduled', 0)
                return f"Ready: {ready}/{desired}"
            except json.JSONDecodeError:
                return "Error: Failed to parse toolkit status"
        else:
            return f"Error: Toolkit not found"
    
    def check_gpu_operator_helm_config(self) -> dict:
        """Check NVIDIA GPU Operator Helm chart configuration for toolkit settings"""
        config_status = {
            'helm_release_found': False,
            'toolkit_config_valid': False,
            'missing_configs': [],
            'errors': []
        }
        
        try:
            # Check if Helm is available
            helm_check = subprocess.run(['helm', 'version'], capture_output=True, text=True)
            if helm_check.returncode != 0:
                config_status['errors'].append("Helm not available or not configured")
                return config_status
            
            # Get Helm releases in gpu-operator namespace
            success, output = self._run_kubectl([
                'get', 'helmreleases.helm.fluxcd.io', '-n', 'gpu-operator', '-o', 'json'
            ])
            
            if not success:
                # Try alternative: check for Helm releases using helm command
                try:
                    helm_list = subprocess.run([
                        'helm', 'list', '-n', 'gpu-operator', '-o', 'json'
                    ], capture_output=True, text=True, check=True)
                    
                    helm_releases = json.loads(helm_list.stdout)
                    gpu_operator_release = None
                    
                    for release in helm_releases:
                        if 'gpu-operator' in release.get('name', ''):
                            gpu_operator_release = release
                            break
                    
                    if gpu_operator_release:
                        config_status['helm_release_found'] = True
                        # Get values for the release
                        try:
                            values_output = subprocess.run([
                                'helm', 'get', 'values', gpu_operator_release['name'], 
                                '-n', 'gpu-operator', '-o', 'yaml'
                            ], capture_output=True, text=True, check=True)
                            
                            values = yaml.safe_load(values_output.stdout)
                            config_status.update(self._validate_toolkit_config(values))
                            
                        except subprocess.CalledProcessError as e:
                            config_status['errors'].append(f"Failed to get Helm values: {e.stderr}")
                        except yaml.YAMLError as e:
                            config_status['errors'].append(f"Failed to parse Helm values: {e}")
                    
                except subprocess.CalledProcessError:
                    config_status['errors'].append("No GPU Operator Helm release found")
                except json.JSONDecodeError:
                    config_status['errors'].append("Failed to parse Helm list output")
            else:
                # Parse FluxCD HelmRelease
                try:
                    helmreleases = json.loads(output)
                    gpu_operator_hr = None
                    
                    for hr in helmreleases.get('items', []):
                        if 'gpu-operator' in hr.get('metadata', {}).get('name', ''):
                            gpu_operator_hr = hr
                            break
                    
                    if gpu_operator_hr:
                        config_status['helm_release_found'] = True
                        values = gpu_operator_hr.get('spec', {}).get('values', {})
                        config_status.update(self._validate_toolkit_config(values))
                    else:
                        config_status['errors'].append("No GPU Operator HelmRelease found")
                        
                except json.JSONDecodeError:
                    config_status['errors'].append("Failed to parse HelmRelease data")
        
        except Exception as e:
            config_status['errors'].append(f"Unexpected error checking Helm config: {e}")
        
        return config_status
    
    def _validate_toolkit_config(self, values: dict) -> dict:
        """Validate toolkit configuration against required settings"""
        result = {
            'toolkit_config_valid': True,
            'missing_configs': [],
            'config_details': {}
        }
        
        # Required toolkit configuration
        required_toolkit_config = {
            'enabled': True,
            'image': 'container-toolkit',
            'imagePullPolicy': 'IfNotPresent',
            'installDir': '/usr/local/nvidia',
            'repository': 'nvcr.io/nvidia/k8s',
            'version': 'v1.17.5-ubuntu20.04'
        }
        
        # Required environment variables
        required_env_vars = [
            {'name': 'CONTAINERD_CONFIG', 'value': '/var/lib/k8s-containerd/k8s-containerd/etc/containerd/config.toml'},
            {'name': 'CONTAINERD_SOCKET', 'value': '/var/lib/k8s-containerd/k8s-containerd/run/containerd/containerd.sock'},
            {'name': 'CONTAINERD_RUNTIME_CLASS', 'value': 'nvidia'}
        ]
        
        # Navigate to toolkit config in values
        toolkit_config = values.get('toolkit', {})
        result['config_details'] = toolkit_config.copy()
        
        # Check if toolkit is enabled
        if not toolkit_config.get('enabled', False):
            result['missing_configs'].append("toolkit.enabled should be true")
            result['toolkit_config_valid'] = False
        
        # Check required fields
        for field, expected_value in required_toolkit_config.items():
            if field == 'enabled':
                continue  # Already checked above
            
            actual_value = toolkit_config.get(field)
            if actual_value != expected_value:
                result['missing_configs'].append(
                    f"toolkit.{field} should be '{expected_value}', found: '{actual_value}'"
                )
                result['toolkit_config_valid'] = False
        
        # Check environment variables
        env_vars = toolkit_config.get('env', [])
        if not isinstance(env_vars, list):
            result['missing_configs'].append("toolkit.env should be a list")
            result['toolkit_config_valid'] = False
        else:
            # Check for required environment variables
            for required_env in required_env_vars:
                env_found = False
                for env_var in env_vars:
                    if (isinstance(env_var, dict) and 
                        env_var.get('name') == required_env['name'] and 
                        env_var.get('value') == required_env['value']):
                        env_found = True
                        break
                
                if not env_found:
                    result['missing_configs'].append(
                        f"toolkit.env missing: {required_env['name']}={required_env['value']}"
                    )
                    result['toolkit_config_valid'] = False
        
        return result
    
    def check_local_node_config(self) -> List[ContainerdConfig]:
        """Check containerd config on local node (when running directly on a node)"""
        configs = []
        
        # Prioritized config paths
        config_paths = [
            '/var/lib/k8s-containerd/k8s-containerd/etc/containerd/config.toml',
            '/etc/containerd/config.toml',
            '/var/lib/rancher/k3s/agent/etc/containerd/config.toml'
        ]
        
        for config_path in config_paths:
            if Path(config_path).exists():
                try:
                    with open(config_path, 'r') as f:
                        config_content = f.read()
                    
                    # Check for nvidia runtime configuration (same logic as GPU failure detector)
                    nvidia_configured = 'nvidia' in config_content and 'runc' in config_content
                    
                    # Extract binary name
                    binary_name = ''
                    binary_exists = False
                    for line in config_content.split('\n'):
                        if 'BinaryName' in line and '=' in line:
                            parts = line.split('=', 1)
                            if len(parts) == 2:
                                binary_name = parts[1].strip().strip('"').strip("'").strip()
                                binary_exists = Path(binary_name).exists()
                                break
                    
                    configs.append(ContainerdConfig(
                        node_name='localhost',
                        config_path=config_path,
                        exists=True,
                        nvidia_runtime_configured=nvidia_configured,
                        config_content=config_content,
                        binary_name=binary_name,
                        binary_exists=binary_exists,
                        binary_path_used=binary_name,
                        config_valid=True
                    ))
                    
                except Exception as e:
                    configs.append(ContainerdConfig(
                        node_name='localhost',
                        config_path=config_path,
                        exists=True,
                        nvidia_runtime_configured=False,
                        config_content='',
                        config_valid=False,
                        error=f"Failed to read: {e}"
                    ))
        
        if not configs:
            configs.append(ContainerdConfig(
                node_name='localhost',
                config_path='',
                exists=False,
                nvidia_runtime_configured=False,
                config_content='',
                error='No containerd config found'
            ))
        
        return configs
        
    def get_cluster_nodes(self) -> List[NodeInfo]:
        """Get all nodes in the cluster with GPU information"""
        try:
            # Get nodes
            result = subprocess.run([
                'kubectl', 'get', 'nodes', '-o', 'json'
            ], capture_output=True, text=True, check=True)
            
            nodes_data = json.loads(result.stdout)
            nodes = []
            
            for node in nodes_data['items']:
                name = node['metadata']['name']
                
                # Get node roles
                labels = node['metadata'].get('labels', {})
                roles = []
                for label, value in labels.items():
                    if 'node-role.kubernetes.io' in label:
                        role = label.split('/')[-1]
                        roles.append(role)
                
                # Get node status
                conditions = node.get('status', {}).get('conditions', [])
                status = 'Unknown'
                for condition in conditions:
                    if condition.get('type') == 'Ready':
                        status = 'Ready' if condition.get('status') == 'True' else 'NotReady'
                        break
                
                # Check if node has GPU resources
                allocatable = node.get('status', {}).get('allocatable', {})
                has_gpu = 'nvidia.com/gpu' in allocatable
                
                # Count GPU pods on this node
                gpu_pods = self._count_gpu_pods_on_node(name)
                
                nodes.append(NodeInfo(
                    name=name,
                    roles=roles or ['worker'],
                    status=status,
                    gpu_pods=gpu_pods,
                    has_gpu_resources=has_gpu
                ))
            
            return nodes
            
        except Exception as e:
            print(f"Error getting cluster nodes: {e}")
            return []
    
    def _count_gpu_pods_on_node(self, node_name: str) -> int:
        """Count GPU pods on a specific node"""
        try:
            result = subprocess.run([
                'kubectl', 'get', 'pods', '--all-namespaces', 
                '--field-selector', f'spec.nodeName={node_name}',
                '-o', 'json'
            ], capture_output=True, text=True)
            
            if result.returncode != 0:
                return 0
            
            pods_data = json.loads(result.stdout)
            gpu_pod_count = 0
            
            for pod in pods_data.get('items', []):
                containers = pod.get('spec', {}).get('containers', [])
                for container in containers:
                    resources = container.get('resources', {})
                    requests = resources.get('requests', {})
                    if 'nvidia.com/gpu' in requests:
                        gpu_pod_count += 1
                        break
            
            return gpu_pod_count
            
        except Exception:
            return 0
    
    def create_debug_pod_spec(self, node_name: str) -> Dict:
        """Create debug pod spec for a specific node"""
        pod_name = f"{self.debug_pod_prefix}-{node_name.replace('.', '-')}"
        
        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": pod_name,
                "namespace": self.namespace,
                "labels": {
                    "app": "gpu-cluster-debug",
                    "node": node_name
                }
            },
            "spec": {
                "nodeName": node_name,
                "hostNetwork": True,
                "hostPID": True,
                "hostIPC": True,
                "restartPolicy": "Never",
                "tolerations": [
                    {"operator": "Exists"}
                ],
                "containers": [
                    {
                        "name": "debug",
                        "image": "python:3.9-slim",
                        "command": ["/bin/sleep", "300"],
                        "securityContext": {
                            "privileged": True
                        },
                        "volumeMounts": [
                            {
                                "name": "host-etc",
                                "mountPath": "/host/etc",
                                "readOnly": True
                            },
                            {
                                "name": "host-var",
                                "mountPath": "/host/var",
                                "readOnly": True
                            },
                            {
                                "name": "host-usr",
                                "mountPath": "/host/usr",
                                "readOnly": True
                            },
                            {
                                "name": "host-dev",
                                "mountPath": "/host/dev",
                                "readOnly": True
                            }
                        ]
                    }
                ],
                "volumes": [
                    {
                        "name": "host-etc",
                        "hostPath": {"path": "/etc"}
                    },
                    {
                        "name": "host-var",
                        "hostPath": {"path": "/var"}
                    },
                    {
                        "name": "host-usr",
                        "hostPath": {"path": "/usr"}
                    },
                    {
                        "name": "host-dev",
                        "hostPath": {"path": "/dev"}
                    }
                ]
            }
        }
    
    def deploy_debug_pod(self, node_name: str) -> Tuple[bool, str]:
        """Deploy debug pod on a specific node"""
        pod_name = f"{self.debug_pod_prefix}-{node_name.replace('.', '-')}"
        
        try:
            # Check if pod already exists
            result = subprocess.run([
                'kubectl', 'get', 'pod', pod_name, '-n', self.namespace
            ], capture_output=True, text=True)
            
            if result.returncode == 0:
                return True, pod_name
            
            # Create pod
            pod_spec = self.create_debug_pod_spec(node_name)
            
            with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
                yaml.dump(pod_spec, f)
                pod_file = f.name
            
            result = subprocess.run([
                'kubectl', 'apply', '-f', pod_file
            ], capture_output=True, text=True)
            
            Path(pod_file).unlink()
            
            if result.returncode != 0:
                return False, f"Failed to create pod: {result.stderr}"
            
            # Wait for pod to be ready (with timeout)
            print(f"  ⏳ Waiting for pod to be ready...")
            for attempt in range(60):  # 60 second timeout
                result = subprocess.run([
                    'kubectl', 'get', 'pod', pod_name, '-n', self.namespace,
                    '-o', 'jsonpath={.status.phase}'
                ], capture_output=True, text=True)
                
                if result.stdout == 'Running':
                    # Double check containers are ready
                    result = subprocess.run([
                        'kubectl', 'get', 'pod', pod_name, '-n', self.namespace,
                        '-o', 'jsonpath={.status.containerStatuses[0].ready}'
                    ], capture_output=True, text=True)
                    
                    if result.stdout == 'true':
                        return True, pod_name
                
                time.sleep(1)
            
            # Check why pod failed
            result = subprocess.run([
                'kubectl', 'describe', 'pod', pod_name, '-n', self.namespace
            ], capture_output=True, text=True)
            
            return False, f"Pod failed to start within timeout. Details: {result.stdout[-500:]}"
            
        except Exception as e:
            return False, f"Exception deploying pod: {e}"
    
    def get_containerd_config_from_node(self, node_name: str, pod_name: str) -> List[ContainerdConfig]:
        """Extract containerd configuration from a specific node"""
        configs = []
        
        # Define paths to check for containerd config (prioritized order)
        config_paths = [
            '/host/var/lib/k8s-containerd/k8s-containerd/etc/containerd/config.toml',
            '/host/etc/containerd/config.toml',
            '/host/var/lib/rancher/k3s/agent/etc/containerd/config.toml',
            '/host/etc/k3s/containerd/config.toml',
            '/host/var/lib/containerd/config.toml'
        ]
        
        for config_path in config_paths:
            try:
                # Check if file exists
                result = subprocess.run([
                    'kubectl', 'exec', '-n', self.namespace, pod_name, '--',
                    'test', '-f', config_path
                ], capture_output=True)
                
                config = ContainerdConfig(
                    node_name=node_name,
                    config_path=config_path,
                    exists=result.returncode == 0,
                    nvidia_runtime_configured=False,
                    config_content=""
                )
                
                if config.exists:
                    # Read the config file
                    result = subprocess.run([
                        'kubectl', 'exec', '-n', self.namespace, pod_name, '--',
                        'cat', config_path
                    ], capture_output=True, text=True)
                    
                    if result.returncode == 0:
                        config.config_content = result.stdout
                        
                        # Check for nvidia runtime configuration
                        # Using same logic as local GPU failure detector
                        content_lower = config.config_content.lower()
                        config.nvidia_runtime_configured = (
                            'nvidia' in content_lower and 
                            'runc' in content_lower
                        )
                        
                        # Extract BinaryName and verify binary exists
                        if config.nvidia_runtime_configured:
                            binary_info = self._extract_and_verify_binary(
                                config.config_content, 
                                pod_name
                            )
                            config.binary_name = binary_info.get('binary_name', '')
                            config.binary_exists = binary_info.get('binary_exists', False)
                            config.binary_path_used = binary_info.get('binary_path', '')
                    else:
                        config.error = f"Failed to read file: {result.stderr}"
                
                configs.append(config)
                
            except Exception as e:
                configs.append(ContainerdConfig(
                    node_name=node_name,
                    config_path=config_path,
                    exists=False,
                    nvidia_runtime_configured=False,
                    config_content="",
                    error=f"Exception: {e}"
                ))
        
        return configs
    
    def _extract_and_verify_binary(self, config_content: str, pod_name: str) -> dict:
        """Extract BinaryName from config and verify if binary exists"""
        binary_info = {
            'binary_name': '',
            'binary_exists': False,
            'binary_path': ''
        }
        
        try:
            # Extract BinaryName from config
            for line in config_content.split('\n'):
                if 'BinaryName' in line and '=' in line:
                    # Extract the path from BinaryName = "path"
                    parts = line.split('=', 1)
                    if len(parts) == 2:
                        binary_path = parts[1].strip().strip('"').strip("'").strip()
                        binary_info['binary_name'] = binary_path
                        
                        # Simple check: prepend /host to the binary path
                        host_binary_path = f'/host{binary_path}'
                        
                        result = subprocess.run([
                            'kubectl', 'exec', '-n', self.namespace, pod_name, '--',
                            'test', '-f', host_binary_path
                        ], capture_output=True, timeout=10)
                        
                        binary_info['binary_exists'] = (result.returncode == 0)
                        binary_info['binary_path'] = binary_path
                        
                        break  # Found BinaryName, stop searching
            
        except Exception as e:
            binary_info['error'] = str(e)
        
        return binary_info
    
    def run_gpu_detection_on_node(self, node_name: str, pod_name: str) -> str:
        """Run GPU failure detection script on a specific node"""
        try:
            # Create a simplified detection script with proper escaping
            detection_script = r'''
import subprocess
import json
import re
from pathlib import Path

def check_containerd_logs():
    try:
        result = subprocess.run([
            "chroot", "/host", "journalctl", "-u", "containerd", 
            "--since", "1 hour ago", "--no-pager", "-q"
        ], capture_output=True, text=True, timeout=30)
        
        errors = []
        if result.returncode == 0:
            lines = result.stdout.split('\n')
            for line in lines:
                if "error" in line.lower() and ("nvidia" in line.lower() or "runtime" in line.lower()):
                    errors.append(line.strip())
        return errors
    except Exception as e:
        return [f"Could not access containerd logs: {str(e)}"]

def check_nvidia_devices():
    try:
        result = subprocess.run([
            "chroot", "/host", "ls", "-la", "/dev/nvidia*"
        ], capture_output=True, text=True, timeout=10)
        return result.stdout if result.returncode == 0 else "No NVIDIA devices found"
    except Exception as e:
        return f"Could not check NVIDIA devices: {str(e)}"

def check_containerd_config():
    config_path = "/host/etc/containerd/config.toml"
    try:
        if Path(config_path).exists():
            with open(config_path, 'r') as f:
                content = f.read()
            return f"Config exists ({len(content)} chars)"
        else:
            return "Config file not found"
    except Exception as e:
        return f"Error reading config: {str(e)}"

def check_nvidia_runtime_binary():
    """Check for NVIDIA runtime binary in standard locations"""
    paths = [
        "/host/usr/local/nvidia/toolkit/nvidia-container-runtime",
        "/host/usr/bin/nvidia-container-runtime",
        "/host/usr/local/bin/nvidia-container-runtime"
    ]
    
    found = []
    for path in paths:
        try:
            result = subprocess.run(["test", "-f", path], capture_output=True, timeout=5)
            if result.returncode == 0:
                # Check if executable
                result_exec = subprocess.run(["test", "-x", path], capture_output=True, timeout=5)
                status = "executable" if result_exec.returncode == 0 else "not executable"
                found.append(f"{path.replace('/host', '')} ({status})")
        except:
            pass
    
    if found:
        return "Found at:\n      " + "\n      ".join(found)
    return "Not found in standard locations"

# Run checks
print("=== GPU DETECTION RESULTS ===")
try:
    hostname_result = subprocess.run(['hostname'], capture_output=True, text=True, timeout=5)
    print(f"Node: {hostname_result.stdout.strip()}")
except:
    print("Node: Unknown")
print()

print("Containerd Config:")
print(f"  {check_containerd_config()}")
print()

print("NVIDIA Runtime Binary:")
print(f"  {check_nvidia_runtime_binary()}")
print()

print("Containerd Errors (last hour):")
errors = check_containerd_logs()
if errors:
    for error in errors[:5]:
        print(f"  {error}")
else:
    print("  No containerd errors found")
print()

print("NVIDIA Devices:")
devices = check_nvidia_devices()
for line in str(devices).split('\n')[:10]:
    if line.strip():
        print(f"  {line}")
print()

print("=== END RESULTS ===")
'''
            
            # Encode and execute the script
            script_b64 = base64.b64encode(detection_script.encode()).decode()
            
            result = subprocess.run([
                'kubectl', 'exec', '-n', self.namespace, pod_name, '--',
                'sh', '-c', f'echo "{script_b64}" | base64 -d | python3 2>&1'
            ], capture_output=True, text=True, timeout=90)
            
            return result.stdout + result.stderr
            
        except subprocess.TimeoutExpired:
            return "GPU detection timed out"
        except Exception as e:
            return f"GPU detection failed: {e}"
    
    def analyze_local_node(self) -> NodeGPUStatus:
        """Analyze GPU configuration on local node"""
        import socket
        node_name = socket.gethostname()
        
        print(f"🔍 Analyzing local node: {node_name}")
        
        # Get containerd configurations
        containerd_configs = self.check_local_node_config()
        
        # Get containerd errors
        containerd_errors = []
        try:
            result = subprocess.run([
                'journalctl', '-u', 'containerd', '--since', '1 hour ago', 
                '--no-pager', '-q'
            ], capture_output=True, text=True, timeout=30)
            
            if result.returncode == 0:
                lines = result.stdout.split('\n')
                for line in lines:
                    if 'error' in line.lower() and ('nvidia' in line.lower() or 'runtime' in line.lower()):
                        containerd_errors.append(line.strip())
        except Exception as e:
            containerd_errors.append(f"Could not retrieve logs: {e}")
        
        # Check for NVIDIA devices
        nvidia_devices = []
        try:
            result = subprocess.run(['ls', '-la', '/dev/nvidia*'], 
                                  capture_output=True, text=True, shell=True)
            if result.returncode == 0:
                nvidia_devices = result.stdout.strip().split('\n')
        except:
            pass
        
        # Format GPU symptoms
        gpu_symptoms = f"""=== LOCAL NODE GPU DETECTION ===
Node: {node_name}

Containerd Errors (last hour):
"""
        if containerd_errors:
            for error in containerd_errors[:5]:
                gpu_symptoms += f"  {error}\n"
        else:
            gpu_symptoms += "  No containerd errors found\n"
        
        gpu_symptoms += "\nNVIDIA Devices:\n"
        if nvidia_devices:
            for device in nvidia_devices[:5]:
                gpu_symptoms += f"  {device}\n"
        else:
            gpu_symptoms += "  No NVIDIA devices found\n"
        
        return NodeGPUStatus(
            node_name=node_name,
            containerd_configs=containerd_configs,
            gpu_failure_symptoms=gpu_symptoms,
            debug_pod_deployed=False
        )
        """Analyze a single node's GPU configuration"""
        print(f"🔍 Analyzing node: {node.name}")
        
        # Deploy debug pod
        success, pod_name_or_error = self.deploy_debug_pod(node.name)
        
        if not success:
            return NodeGPUStatus(
                node_name=node.name,
                containerd_configs=[],
                debug_pod_deployed=False,
                execution_error=pod_name_or_error
            )
        
        pod_name = pod_name_or_error
        
        try:
            # Get containerd configurations
            containerd_configs = self.get_containerd_config_from_node(node.name, pod_name)
            
            # Run GPU detection
            gpu_symptoms = self.run_gpu_detection_on_node(node.name, pod_name)
            
            return NodeGPUStatus(
                node_name=node.name,
                containerd_configs=containerd_configs,
                gpu_failure_symptoms=gpu_symptoms,
                debug_pod_deployed=True
            )
            
        except Exception as e:
            return NodeGPUStatus(
                node_name=node.name,
                containerd_configs=[],
                debug_pod_deployed=True,
                execution_error=f"Analysis failed: {e}"
            )
    
    def analyze_single_node(self, node: NodeInfo) -> NodeGPUStatus:
        """Analyze a single node's GPU configuration"""
        print(f"🔍 Analyzing node: {node.name}")
        
        # Deploy debug pod
        success, pod_name_or_error = self.deploy_debug_pod(node.name)
        
        if not success:
            return NodeGPUStatus(
                node_name=node.name,
                containerd_configs=[],
                debug_pod_deployed=False,
                execution_error=pod_name_or_error
            )
        
        pod_name = pod_name_or_error
        
        try:
            # Get containerd configurations
            containerd_configs = self.get_containerd_config_from_node(node.name, pod_name)
            
            # Run GPU detection
            gpu_symptoms = self.run_gpu_detection_on_node(node.name, pod_name)
            
            return NodeGPUStatus(
                node_name=node.name,
                containerd_configs=containerd_configs,
                gpu_failure_symptoms=gpu_symptoms,
                debug_pod_deployed=True
            )
            
        except Exception as e:
            return NodeGPUStatus(
                node_name=node.name,
                containerd_configs=[],
                debug_pod_deployed=True,
                execution_error=f"Analysis failed: {e}"
            )
    
    def cleanup_debug_pods(self, nodes: List[NodeInfo]):
        """Clean up all debug pods"""
        print("\n🧹 Cleaning up debug pods...")
        
        for node in nodes:
            pod_name = f"{self.debug_pod_prefix}-{node.name.replace('.', '-')}"
            try:
                subprocess.run([
                    'kubectl', 'delete', 'pod', pod_name, '-n', self.namespace,
                    '--ignore-not-found=true'
                ], capture_output=True, timeout=30)
            except:
                pass
    
    def analyze_cluster(self, cleanup: bool = True) -> Dict[str, NodeGPUStatus]:
        """Analyze GPU configuration across all nodes in the cluster"""
        print("🚀 Starting cluster-wide GPU configuration analysis...")
        
        # Get all nodes
        nodes = self.get_cluster_nodes()
        if not nodes:
            print("❌ No nodes found in cluster")
            return {}
        
        print(f"📊 Found {len(nodes)} nodes in cluster")
        
        # Analyze nodes in parallel
        results = {}
        
        try:
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                # Submit all tasks
                future_to_node = {
                    executor.submit(self.analyze_single_node, node): node
                    for node in nodes
                }
                
                # Collect results
                for future in as_completed(future_to_node):
                    node = future_to_node[future]
                    try:
                        result = future.result(timeout=120)
                        results[node.name] = result
                        print(f"✅ Completed analysis for {node.name}")
                    except Exception as e:
                        print(f"❌ Failed to analyze {node.name}: {e}")
                        results[node.name] = NodeGPUStatus(
                            node_name=node.name,
                            containerd_configs=[],
                            execution_error=f"Analysis exception: {e}"
                        )
        
        finally:
            if cleanup:
                self.cleanup_debug_pods(nodes)
        
        return results
    
    def print_cluster_report(self, results: Dict[str, NodeGPUStatus]):
        """Print comprehensive cluster GPU configuration report"""
        print("\n" + "=" * 100)
        print("CLUSTER-WIDE GPU CONFIGURATION ANALYSIS REPORT")
        print("=" * 100)
        
        # Get cluster-level status information
        pending_gpu_pods = self.get_pending_gpu_pods()
        device_plugin_status = self.check_nvidia_device_plugin_status()
        toolkit_status = self.check_nvidia_container_toolkit_status()
        helm_config_status = self.check_gpu_operator_helm_config()
        runtime_config_issues = self.get_runtime_config_issues(results)
        
        # Cluster-level status sections
        print(f"\n📋 CLUSTER-LEVEL STATUS:")
        print("=" * 100)
        
        print("\nPENDING GPU PODS:")
        print("-" * 40)
        if pending_gpu_pods:
            for pod in pending_gpu_pods:
                print(f"  {pod['namespace']}/{pod['name']}")
                print(f"    Status: {pod['status']}")
                print(f"    Node: {pod['node']}")
                print()
        else:
            print("  No pending GPU pods detected")
        print()
        
        print("CONTAINER RUNTIME ERRORS:")
        print("-" * 40)
        # Collect containerd errors from node detection results
        containerd_errors = []
        for result in results.values():
            if result.gpu_failure_symptoms and 'Containerd Errors' in result.gpu_failure_symptoms:
                lines = result.gpu_failure_symptoms.split('\n')
                for i, line in enumerate(lines):
                    if 'Containerd Errors' in line:
                        # Get next few lines after "Containerd Errors"
                        for error_line in lines[i+1:i+6]:
                            if error_line.strip() and not error_line.startswith('==='):
                                containerd_errors.append(error_line.strip())
        
        if containerd_errors:
            for error in containerd_errors[:10]:  # Show first 10 errors
                print(f"  {error}")
        else:
            print("  No containerd runtime errors detected")
        print()
        
        print("RUNTIME CONFIGURATION ISSUES:")
        print("-" * 40)
        if runtime_config_issues:
            for issue in runtime_config_issues:
                print(f"  {issue}")
        else:
            print("  No runtime configuration issues detected")
        print()
        
        print("NVIDIA DEVICE PLUGIN STATUS:")
        print("-" * 40)
        for key, value in device_plugin_status.items():
            print(f"  {key}: {value}")
        print()
        
        print("NVIDIA CONTAINER TOOLKIT STATUS:")
        print("-" * 40)
        print(f"  {toolkit_status}")
        print()
        
        print("NVIDIA GPU OPERATOR HELM CONFIGURATION:")
        print("-" * 40)
        if helm_config_status['helm_release_found']:
            print(f"  ✅ Helm Release: Found")
            if helm_config_status['toolkit_config_valid']:
                print(f"  ✅ Toolkit Configuration: Valid")
            else:
                print(f"  ❌ Toolkit Configuration: Invalid")
                print("  Missing or incorrect configurations:")
                for missing_config in helm_config_status['missing_configs']:
                    print(f"    • {missing_config}")
        else:
            print(f"  ❌ Helm Release: Not found")
        
        if helm_config_status['errors']:
            print("  Errors:")
            for error in helm_config_status['errors']:
                print(f"    • {error}")
        
        if helm_config_status.get('config_details'):
            print("  Current toolkit configuration:")
            config_details = helm_config_status['config_details']
            for key, value in config_details.items():
                if key == 'env' and isinstance(value, list):
                    print(f"    {key}:")
                    for env_var in value:
                        if isinstance(env_var, dict):
                            print(f"      - {env_var.get('name', '')}: {env_var.get('value', '')}")
                        else:
                            print(f"      - {env_var}")
                else:
                    print(f"    {key}: {value}")
        print()
        
        # Summary
        print(f"\n📊 NODE-LEVEL SUMMARY:")
        print("=" * 100)
        total_nodes = len(results)
        nodes_with_nvidia_runtime = 0
        nodes_with_errors = 0
        nodes_with_configs = 0
        nodes_with_missing_binary = 0
        
        for result in results.values():
            if result.execution_error:
                nodes_with_errors += 1
                continue
                
            has_config = any(c.exists for c in result.containerd_configs)
            has_nvidia = any(c.nvidia_runtime_configured for c in result.containerd_configs)
            
            if has_config:
                nodes_with_configs += 1
            if has_nvidia:
                nodes_with_nvidia_runtime += 1
                
                # Check for missing binary
                for config in result.containerd_configs:
                    if config.nvidia_runtime_configured and config.binary_name and not config.binary_exists:
                        nodes_with_missing_binary += 1
                        break
        
        print(f"\n📊 SUMMARY:")
        print(f"  Total nodes analyzed: {total_nodes}")
        print(f"  Nodes with containerd configs: {nodes_with_configs}")
        print(f"  Nodes with NVIDIA runtime configured: {nodes_with_nvidia_runtime}")
        print(f"  Nodes with missing NVIDIA binary: {nodes_with_missing_binary}")
        print(f"  Nodes with analysis errors: {nodes_with_errors}")
        
        # Detailed per-node analysis
        print(f"\n📋 DETAILED NODE ANALYSIS:")
        print("-" * 100)
        
        for node_name, result in results.items():
            print(f"\n🖥️  NODE: {node_name}")
            print("-" * 50)
            
            if result.execution_error:
                print(f"  ❌ Error: {result.execution_error}")
                continue
            
            if not result.debug_pod_deployed:
                print(f"  ❌ Debug pod not deployed")
                continue
            
            # Containerd configurations
            print("  📄 Containerd Configurations:")
            
            if not result.containerd_configs:
                print("    ⚠️  No containerd configs found")
            else:
                for config in result.containerd_configs:
                    status_icon = "✅" if config.exists else "❌"
                    nvidia_icon = "🎯" if config.nvidia_runtime_configured else "⭕"
                    
                    print(f"    {status_icon} {config.config_path}")
                    print(f"      Exists: {config.exists}")
                    
                    if config.exists:
                        print(f"      NVIDIA Runtime: {nvidia_icon} {config.nvidia_runtime_configured}")
                        
                        # Show binary verification if NVIDIA runtime is configured
                        if config.nvidia_runtime_configured:
                            if config.binary_name:
                                status = "✅ FOUND" if config.binary_exists else "❌ MISSING"
                                print(f"      Binary: {config.binary_name} {status}")
                            else:
                                print(f"      ⚠️  BinaryName not specified in config")
                        
                        if config.config_content:
                            # Show relevant parts of config
                            lines = config.config_content.split('\n')
                            nvidia_lines = [line.strip() for line in lines if 'nvidia' in line.lower()]
                            runtime_lines = [line.strip() for line in lines if 'runtime' in line.lower() and 'nvidia' in line.lower()]
                            
                            if nvidia_lines or runtime_lines:
                                print("      Config lines:")
                                for line in (nvidia_lines + runtime_lines)[:5]:
                                    print(f"        {line}")
                    
                    if config.error:
                        print(f"      ⚠️  Error: {config.error}")
                    
                    print()
            
            # GPU symptoms
            if result.gpu_failure_symptoms:
                print("  🔍 GPU Detection Results:")
                # Format the symptoms nicely
                symptoms = result.gpu_failure_symptoms.strip()
                if symptoms and not symptoms.startswith("GPU detection"):
                    for line in symptoms.split('\n'):
                        if line.strip():
                            print(f"    {line}")
                elif symptoms.startswith("GPU detection"):
                    print(f"    ⚠️  {symptoms}")
                else:
                    print(f"    No GPU detection results available")
            
            print()  # Add spacing between nodes
        
        # Recommendations
        print(f"\n🔧 CLUSTER-WIDE RECOMMENDATIONS:")
        print("-" * 50)
        
        unconfigured_nodes = []
        error_nodes = []
        missing_binary_nodes = []
        
        for node_name, result in results.items():
            if result.execution_error:
                error_nodes.append(node_name)
                continue
            
            has_nvidia_runtime = any(c.nvidia_runtime_configured for c in result.containerd_configs if c.exists)
            if not has_nvidia_runtime:
                unconfigured_nodes.append(node_name)
            else:
                # Check if binary exists
                for config in result.containerd_configs:
                    if config.nvidia_runtime_configured and config.binary_name and not config.binary_exists:
                        missing_binary_nodes.append({
                            'node': node_name,
                            'binary_path': config.binary_name,
                            'config_path': config.config_path
                        })
        
        if missing_binary_nodes:
            print(f"1. ⚠️  Nodes with missing NVIDIA runtime binary:")
            for node_info in missing_binary_nodes:
                print(f"   • {node_info['node']}: {node_info['binary_path']}")
            print()
            print("   Fix: Ensure NVIDIA Container Toolkit is installed")
            print("   Check: kubectl get pods -n gpu-operator")
            print()
        
        if unconfigured_nodes:
            print(f"2. 🎯 Configure NVIDIA runtime on nodes without it:")
            for node in unconfigured_nodes:
                print(f"   - {node}")
            print("   Add nvidia runtime configuration to containerd config.toml:")
            print("   ")
            print("   [plugins.\"io.containerd.grpc.v1.cri\".containerd.runtimes.nvidia]")
            print("     privileged_without_host_devices = false")
            print("     runtime_type = \"io.containerd.runc.v2\"")
            print("     [plugins.\"io.containerd.grpc.v1.cri\".containerd.runtimes.nvidia.options]")
            print("       BinaryName = \"/usr/local/nvidia/toolkit/nvidia-container-runtime\"")
            print()
        
        if error_nodes:
            print(f"3. 🔧 Investigate nodes with analysis errors:")
            for node in error_nodes:
                print(f"   - {node}")
            print("   Check node accessibility and debug pod deployment")
            print()
        
        print("3. 📝 Verify NVIDIA Container Toolkit installation:")
        print("   kubectl get pods -n gpu-operator -o wide")
        print()
        
        # Add Helm configuration recommendations
        if not helm_config_status['helm_release_found']:
            print("4. 🚀 Install or fix NVIDIA GPU Operator Helm chart:")
            print("   # Check if Helm is available:")
            print("   helm version")
            print("   # Install GPU Operator:")
            print("   helm repo add nvidia https://helm.ngc.nvidia.com/nvidia")
            print("   helm repo update")
            print("   helm install gpu-operator nvidia/gpu-operator -n gpu-operator --create-namespace")
            print()
        elif not helm_config_status['toolkit_config_valid']:
            print("4. 🔧 Fix NVIDIA GPU Operator Helm configuration:")
            print("   Update your Helm values to include the required toolkit configuration:")
            print("   ```yaml")
            print("   toolkit:")
            print("     enabled: true")
            print("     env: []")
            print("     image: container-toolkit")
            print("     imagePullPolicy: IfNotPresent")
            print("     imagePullSecrets: []")
            print("     installDir: /usr/local/nvidia")
            print("     repository: nvcr.io/nvidia/k8s")
            print("     resources: {}")
            print("     version: v1.17.5-ubuntu20.04")
            print("     env:")
            print("     - name: CONTAINERD_CONFIG")
            print("       value: /var/lib/k8s-containerd/k8s-containerd/etc/containerd/config.toml")
            print("     - name: CONTAINERD_SOCKET")
            print("       value: /var/lib/k8s-containerd/k8s-containerd/run/containerd/containerd.sock")
            print("     - name: CONTAINERD_RUNTIME_CLASS")
            print("       value: nvidia")
            print("   ```")
            print("   Then upgrade the Helm release:")
            print("   helm upgrade gpu-operator-1758912452 nvidia/gpu-operator -n gpu-operator -f values.yaml")
            print()
        
        print("5. 🔄 Restart containerd on misconfigured nodes:")
        print("   # SSH to each node and run:")
        print("   sudo systemctl restart containerd")


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description='Cluster-wide GPU Configuration Analyzer')
    parser.add_argument('--namespace', default='kube-system', 
                       help='Namespace for debug pods (default: kube-system)')
    parser.add_argument('--max-workers', type=int, default=5,
                       help='Maximum concurrent node analysis (default: 5)')
    parser.add_argument('--no-cleanup', action='store_true',
                       help='Keep debug pods after analysis')
    
    args = parser.parse_args()
    
    try:
        analyzer = ClusterGPUAnalyzer(
            namespace=args.namespace,
            max_workers=args.max_workers
        )
        
        results = analyzer.analyze_cluster(cleanup=not args.no_cleanup)
        analyzer.print_cluster_report(results)
        
        return 0
        
    except KeyboardInterrupt:
        print("\n⏹️  Analysis interrupted by user")
        return 1
    except Exception as e:
        print(f"\n❌ Analysis failed: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
