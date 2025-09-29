# GPU Workload Failure Diagnosis and Resolution

## Problem Statement
GPU workloads stuck in `ContainerCreating` state due to container runtime misconfiguration in a Kubernetes cluster using snap-based containerd.

## Executive Summary

### What Happened
GPU workload failed because the snap-based containerd instance (`snap.k8s.containerd.service`) couldn't find the "Nvidia" runtime needed for GPU container creation. The system was running two containerd instances with the NVIDIA Container Toolkit configured for the wrong instance.

### Impact
- Training/inference jobs cannot access GPU resources
- Workloads stuck in `ContainerCreating`/`ContainerStatusUnknown` state
- ML workloads unable to utilize GPU acceleration
- Development and production workflows disrupted

## Investigation Process

### Observations

1. Examine events for the `gpu-textgen` workload pod

```bash
root@192-18-132-31:~# k -n default describe pod gpu-textgen
.....
.....
.....
Events:
  Type     Reason                  Age                    From     Message
  ----     ------                  ----                   ----     -------
  Warning  FailedCreatePodSandBox  4m24s (x279 over 64m)  kubelet  Failed to create pod sandbox: rpc error: code = Unknown desc = failed to get sandbox runtime: no runtime for "nvidia" is configured
```

2. Confirmed the containerd instance (`snap.k8s.containerd.service`) configuration and logs

```bash
root@192-18-132-31:~# cat /var/lib/k8s-containerd/k8s-containerd/etc/containerd/config.toml  | grep -i nvidia
```

```bash
root@192-18-132-31:~# journalctl -u snap.k8s.containerd.service -n 100
...
...

Sep 28 03:40:06 192-18-132-31 k8s.containerd[242547]: time="2025-09-28T03:40:06.218563265Z" level=error msg="CreateContainer within sandbox \"dc6f2ba7bdb1fb501c7dd5dcd50a48d9dc68c99274a565ae90befd07d7f26dad\" for &ContainerMetadata{Name:plugin
-validation,Attempt:0,} failed" error="failed to get sandbox runtime: no runtime for \"nvidia\" is configured"
```

3. Observe the NVIDIA system containers and helm-chart configuration 

```bash
 kubectl get pods -n gpu-operator -o wide
NAME                                                              READY   STATUS             RESTARTS        AGE   IP           NODE            NOMINATED NODE   READINESS GATES
gpu-feature-discovery-6hbp9                                       1/1     Running            0               78m   10.1.0.39    192-18-132-31   <none>           <none>
gpu-operator-1758912452-node-feature-discovery-gc-54fc76b7ssrds   1/1     Running            0               78m   10.1.0.68    192-18-132-31   <none>           <none>
gpu-operator-1758912452-node-feature-discovery-master-5cb782bgx   1/1     Running            0               78m   10.1.0.232   192-18-132-31   <none>           <none>
gpu-operator-1758912452-node-feature-discovery-worker-7kv2n       1/1     Running            0               78m   10.1.0.66    192-18-132-31   <none>           <none>
gpu-operator-687f46747c-whzzw                                     1/1     Running            0               78m   10.1.0.201   192-18-132-31   <none>           <none>
nvidia-container-toolkit-daemonset-bhqw6                          0/1     CrashLoopBackOff   14 (3m1s ago)   50m   10.1.0.17    192-18-132-31   <none>           <none>
nvidia-cuda-validator-btjl6                                       0/1     Completed          0               77m   <none>       192-18-132-31   <none>           <none>
nvidia-dcgm-exporter-zb2zm                                        1/1     Running            0               77m   10.1.0.40    192-18-132-31   <none>           <none>
nvidia-device-plugin-daemonset-7npm6                              1/1     Running            0               77m   10.1.0.219   192-18-132-31   <none>           <none>
nvidia-operator-validator-gbcq4                                   1/1     Running            0               77m   10.1.0.154   192-18-132-31   <none>           <none>
```

```bash
root@192-18-132-31:~# helm get values gpu-operator-1758912452 -n gpu-operator --all -o yaml | grep -A20 -i "toolkit:"
  use_ocp_driver_toolkit: false
platform:
  openshift: false
psa:
  enabled: false
sandboxDevicePlugin:
  args: []
  enabled: true
  env: []
  image: kubevirt-gpu-device-plugin
  imagePullPolicy: IfNotPresent
  imagePullSecrets: []
  repository: nvcr.io/nvidia
  resources: {}
  version: v1.3.1
sandboxWorkloads:
  defaultWorkload: container
  enabled: false
toolkit:
  enabled: true
  env: []
  image: container-toolkit
  imagePullPolicy: IfNotPresent
  imagePullSecrets: []
  installDir: /usr/local/nvidia
  repository: nvcr.io/nvidia/k8s
  resources: {}
  version: v1.17.5-ubuntu20.04
validator:
  args: []
  env: []
  image: gpu-operator-validator
  imagePullPolicy: IfNotPresent
  imagePullSecrets: []
  plugin:
    env:
    - name: WITH_WORKLOAD
      value: "false"
  repository: nvcr.io/nvidia/cloud-native
```

### Root Cause Analysis

#### Primary Issue: Snap Containerd Configuration Mismatch
- **Problem**: The snap-based containerd instance (`snap.k8s.containerd.service`) lacks proper NVIDIA runtime configuration
- **Error**: `failed to get sandbox runtime: no runtime for "Nvidia" is configured`
- **Details**: System running 2 containerd instances:
  - Kubernetes one: `snap.k8s.containerd.service` under `/var/lib/k8s-containerd/k8s-containerd/` (non-standard path)
  - Default one: `containerd.service` (standard convention)
  - Helm chart for `gpu-operator-1758912452 -n gpu-operator` is configured for the containerd instance (`snap.k8s.containerd.service`) so Kubernetes and 
    associated containerd won't work correctly. By default helm chart assumes `CONTAINERD_CONFIG` and `CONTAINERD_SOCKET` to `/etc/containerd/config.toml` and `/run/containerd/containerd.sock`

- **Impact**: Kubernetes couldn't start GPU containers or run NVIDIA system containers properly

## Resolution Process

### Automated Detection and Fixing Tool

A comprehensive tool has been developed to automatically detect, fix, and verify the containerd configuration issue:

**Script Location**: [`task2/scripts/cluster_wide_gpu_debug.py`](./scripts/cluster_wide_gpu_debug.py)

##### Usage Options

```bash
# Check for issues only
python3 cluster_wide_gpu_debug.py

# Automatically fix detected issues and dry-run
python3 cluster_wide_gpu_debug.py --fix --dry-run

# Automatically fix detected issue
python3 cluster_wide_gpu_debug.py --fix
🔧 Applying GPU operator fix...                                                                                                                                                                                                                    
✅ Found fixed values file at: /root/fixed_values.yaml                                                                                                                                                                                             
📦 Using GPU operator release: gpu-operator-1758912452                                                                                                                                                                                             
📥 Getting current chart values...                                                                                                                                                                                                                 
Running: helm get values gpu-operator-1758912452 -n gpu-operator --all -o yaml                                                                                                                                                                     
✅ Successfully retrieved current values                                                                                                                                                                                                           
🔄 Merging current values with fixed values...

...
...
...

✅ Successfully merged values                                                                                                                                                                                                                      
📄 Merged values saved to: merged_values.yaml                                                                                                                                                                                                      
🚀 Upgrading GPU operator with merged values...                                                                                                                                                                                                    
Running: helm upgrade gpu-operator-1758912452 nvidia/gpu-operator -n gpu-operator -f merged_values.yaml                                                                                                                                            
✅ GPU operator upgrade completed successfully                                                                                                                                                                                                     
📋 Upgrade output:                                                                                                                                                                                                                                 
Release "gpu-operator-1758912452" has been upgraded. Happy Helming!                                                                                                                                                                                
NAME: gpu-operator-1758912452                                                                                                                                                                                                                      
LAST DEPLOYED: Mon Sep 29 09:09:04 2025                                                                                                                                                                                                            
NAMESPACE: gpu-operator                                                                                                                                                                                                                            
STATUS: deployed                                                                                                                                                                                                                                   
REVISION: 4                                                                                                                                                                                                                                        
TEST SUITE: None 
```

### Manual Resolution Steps

#### Step 1: Identify the Issue
```bash
# Check containerd configuration
cat /var/lib/k8s-containerd/k8s-containerd/etc/containerd/config.toml | grep -A 10 nvidia

# Check for NVIDIA runtime
kubectl get runtimeclass
```

#### Step 2: Fix GPU operator helm chart configuration 
Add the following configuration to GPU operator helm chart:

```yaml

helm get values gpu-operator-1758912452 -n gpu-operator --all -o yaml > config.yaml

#add the following configuration to toolkit.env
toolkit:                                                                                                               
  enabled: true                                   
  env:                                                                                                                 
    - name: CONTAINERD_CONFIG   
      value: /var/lib/k8s-containerd/k8s-containerd/etc/containerd/config.toml            
    - name: CONTAINERD_SOCKET                                                                                          
      value: /var/lib/k8s-containerd/k8s-containerd/run/containerd/containerd.sock
    - name: CONTAINERD_RUNTIME_CLASS
      value: nvidia      

#to verify the values file
helm upgrade gpu-operator-1758912452 nvidia/gpu-operator -n gpu-operator -f config.yaml --dry-run 

helm upgrade gpu-operator-1758912452 nvidia/gpu-operator -n gpu-operator -f config.yaml
```

#### Step 3: Verify Fix
```bash
# Check if NVIDIA runtime is available
kubectl get runtimeclass

# Check GPU workload
kubectl get pods

# check script
python3 cluster_wide_gpu_debug.py --namespace gpu-operator --max-workers 5
```

## Networking issue
Workloads were running now, however failed during the runtime, `gpu-textgen` was terminated with the error.

To understand how Kubernetes networking works, please refer to the [Kubernetes
networking architecture](./nw-architecture.md).

### Observations

1. Pod was in error state and following logs show 'DNS lookup' issue when running pip command

```bash
root@192-18-132-31:~/config/gpu-opertor# k -n default logs gpu-textgen                                                                                                                                                                             
WARNING: Retrying (Retry(total=4, connect=None, read=None, redirect=None, status=None)) after connection broken by 'NewConnectionError('<pip._vendor.urllib3.connection.HTTPSConnection object at 0x773dc193ed70>: Failed to establish a new connec
tion: [Errno -3] Temporary failure in name resolution')': /simple/pip/                                                                                                                                                                      
.....
.....
```

### Secondary Issue: Networking failures

- **Problem**: Upon observing the application logs, workloads were failing to download the python dependencies from internet
- **Error**: `[Errno -3] Temporary failure in name resolution`
- **Details**: As python program trying to download external dependencies from the internet, DNS resolution failed.
  - Essentially either Kubernetes or host networking is not allowing to perform the DNS query or blocking all traffic.
  - Default one: `containerd.service` (standard convention)
- **Impact**: Kubernetes couldn't route the traffic to external world.

#### Root Cause Analysis

- As Kubernetes traffic(coredns) is trying to resolve the DNS name, its been served traffic somehow dropping or issues persist with 
coredns.
- In order to confirm if issues is just with DNS or any other traffic, a test is executed [network-debug-advanced](./scripts/debug.yaml) which runs 
various tests such as ping, dig, curl and tracepoint.
- It's been observed all traffic were blocked originating from the Kubernetes network.
- Upon running the same DNS query from the host, DNS were able to resolve the queries. Which made it clear, an issue is with the Kubernetes networking.
- Cilium traffic has been observed through `kubectl -n kube-system exec -it cilium-dwwp7 -- cilium monitor --from 240`, traffic were originating from 
coredns pod, however no traffic were returning back from the external world
- Running tcpdump, it's been observed the traffic were not reaching to the host
N/W interface
```bash
Sep 28 09:36:20 192-18-132-31 kernel: [54237.164083] [UFW BLOCK] IN=lxc4f39491e6165 OUT=eno1 MAC=ea:34:8a:6c:c4:4f:0a:67:41:64:51:44:08:00 SRC=10.1.0.185 DST=1.1.1.1 LEN=45 TOS=0x00 PREC=0x00 TTL=62 ID=4481 DF PROTO=UDP SPT=59834 DPT=53 LEN=25 MARK=0xa1700f00 
```
-  Possibly it's routing issue or firewall, upon observing firewall logs, it's found traffic is getting blocked from the coredns IP 
```bash
Sep 28 09:37:00 192-18-132-31 kernel: [54277.196906] [UFW BLOCK] IN=lxc4f39491e6165 OUT=eno1 MAC=ea:34:8a:6c:c4:4f:0a:67:41:64:51:44:08:00 SRC=10.1.0.185 DST=8.8.8.8 LEN=45 TOS=0x00 PREC=0x00 TTL=62 ID=33338 DF PROTO=UDP SPT=39480 DPT=53 LEN=25 MARK=0xa1700f00 
```
- This traffic from the cilium host(virtual router) to host interface was blocked by the firewall rules/IP Table rule

### Resolution Process

Following rules were configured to through firewall (ufw).

#### Step 1: Configure the firewall rules
```bash
# Allow incoming traffic from lxc(any container interface) to eno1(host interface)
ufw route allow in on lxc+ out on eno1

# Allow incoming traffic from  eno1(host interface) to lxc(any container interface)
ufw route allow in on eno1 out on lxc+

# Allow incoming traffic from 10.0.0.0/8 (Kubernetes pod/service)
ufw allow from 10.0.0.0/8

# Allow outgoing traffic to 10.0.0.0/8 (Kubernetes pod/service)
ufw allow out to 10.0.0.0/8
```

####  Step 2: Verify the rules
```bash
ufw reload

ufw status verbose
Status: active
Logging: on (medium)
Default: deny (incoming), allow (outgoing), deny (routed)
New profiles: skip

To                         Action      From
--                         ------      ----
22/tcp                     ALLOW IN    Anywhere                  
Anywhere                   ALLOW IN    10.0.0.0/8                
22/tcp (v6)                ALLOW IN    Anywhere (v6)             

10.0.0.0/8                 ALLOW OUT   Anywhere                  

Anywhere on eno1           ALLOW FWD   Anywhere on lxc+          
Anywhere on lxc+           ALLOW FWD   Anywhere on eno1          
Anywhere (v6) on eno1      ALLOW FWD   Anywhere (v6) on lxc+     
Anywhere (v6) on lxc+      ALLOW FWD   Anywhere (v6) on eno1     
```

####  Step 3: Recreate the pod using `/home/ubuntu/gpu-workload.yaml`
```bash
root@192-18-132-31:~# k delete -f /home/ubuntu/gpu-workload.yaml 
pod "gpu-textgen" deleted from default namespace
root@192-18-132-31:~# k create -f /home/ubuntu/gpu-workload.yaml 
pod/gpu-textgen created
```

####  Step 3: Confirm the result
```bash
root@192-18-132-31:~# k get pods
NAME                     READY   STATUS      RESTARTS   AGE
gpu-textgen              0/1     Completed   0          50s

root@192-18-132-31:~# k logs gpu-textgen
WARNING: Running pip as the 'root' user can result in broken permissions and conflicting behaviour with the system package manager. It is recommended to use a virtual environment instead: https://pip.pypa.io/warnings/venv
WARNING: Running pip as the 'root' user can result in broken permissions and conflicting behaviour with the system package manager, possibly rendering your system unusable. It is recommended to use a virtual environment instead: https://pip.pypa.io/warnings/venv. Use the --root-user-action option if you know what you are doing and want to suppress this warning.
`torch_dtype` is deprecated! Use `dtype` instead!
OK: /outputs/textgen.tx
```

### Prevention Measures
- **Automated Detection**: The cluster now has automated tools to detect this type of issue
- **Monitoring**: Added alerts for GPU configuration problems
- **Documentation**: Updated runbooks for troubleshooting GPU issues

### If You Encounter Similar Issues
1. **Check Pod Status**: `kubectl get pods` - look for "ContainerCreating" or "Error" states
2. **Check Logs**: `kubectl describe pod <pod-name>` for detailed error messages
3. **Contact Platform Team**: We have automated tools to fix most GPU configuration issues
4. **Use the Diagnostic Tool**: `python3 cluster_wide_gpu_debug.py` to diagnose issues

### Mitigation Strategies
1. **Configuration Management**: Version control for containerd configurations
2. **Regular Audits**: Automated checks for configuration drift
3. **Access Controls**: Proper RBAC for GPU resource allocation
4. **Monitoring**: Continuous monitoring of GPU resource usage and access patterns

## Communication with ML Ops Engineers

### Quick Response Process

When GPU workloads fail, follow this simple communication process:

#### 1. Initial Response (0-5 minutes)
- Create tracking ticket
- Set up Slack channel: `#gpu-issues-[date]`
- Notify ML Ops team

#### 2. Regular Updates (Every 15 minutes)
```
🔄 UPDATE - [Time]
Status: [Investigating/Fixing/Testing/Resolved]
Progress: [What we found/did]
ETA: [Expected fix time]
```

#### 3. Resolution Confirmation
```
✅ FIXED - [Time]
Issue: GPU workloads stuck in ContainerCreating
Solution: Fixed containerd config + firewall rules
Status: All ML jobs can run normally
```

#### Resolution Summary
```
Subject: RESOLVED: GPU Issues Fixed

Status: FIXED
Root Cause: Containerd misconfiguration + networking issues
Solution: Updated GPU operator config + firewall rules

All GPU workloads are now functional. Please retry failed ML jobs.

[Platform Team]
```

#### Technical Details (For ML Engineers)
```
**Root Cause**: 
- Snap-based containerd instance lacks proper NVIDIA runtime configuration
- System running two containerd instances with NVIDIA Container Toolkit configured for the wrong instance
- Kubernetes couldn't start GPU containers or run NVIDIA system containers properly

**Resolution Applied**:
- Updated GPU operator helm chart configuration
- Fixed containerd runtime configuration paths
- Configured proper firewall rules for Kubernetes networking

**Verification Steps**:
- GPU workloads now successfully create containers
- DNS resolution working for external dependencies
- All GPU resources accessible to ML workloads
```

#### Follow-up Communication (After Resolution)
```
Subject: GPU Job Failures - RESOLVED

Hi [ML Team],

✅ **RESOLUTION COMPLETE**

The GPU job failures have been resolved. Here's what was fixed:

1. **Container Runtime**: Fixed containerd configuration for NVIDIA runtime
2. **Networking**: Resolved DNS resolution issues blocking external dependencies
3. **Firewall**: Updated rules to allow Kubernetes pod traffic

**What This Means for You**:
- All GPU workloads should now run successfully
- No changes needed to your existing job configurations
- GPU resources are fully available for training/inference

**Prevention**: We've implemented automated monitoring to prevent similar issues in the future.

Please retry your failed jobs - they should complete successfully now.

If you encounter any issues, please let us know immediately.

Best regards,
[Platform Team]
```

#### Key Points to Emphasize
1. **Transparency**: Explain what went wrong in simple terms
2. **Impact**: Clearly state how it affects their work
3. **Resolution**: Describe what was fixed and how
4. **Timeline**: Provide clear expectations for resolution
5. **Prevention**: Mention steps taken to prevent recurrence
6. **Support**: Offer immediate help if issues persist

#### Communication Channels
- **Immediate**: Slack/Teams message for urgent issues
- **Status Updates**: Regular updates during resolution process
- **Post-Resolution**: Follow-up to confirm everything is working
- **Detailed**: Page/Document contains every details of issues and resulution 
with the timeline

## Next Steps

### Immediate Actions
1. **Monitor**: Watch for any recurring GPU configuration issues
2. **Document**: Update team documentation with new troubleshooting procedures
3. **Train**: Brief team on new diagnostic tools and procedures

### Long-term Improvements
1. **Automated Testing**: Regular automated tests for GPU configuration
2. **Enhanced Monitoring**: More comprehensive GPU resource monitoring
3. **Team Training**: Regular training sessions on GPU troubleshooting
4. **Process Improvement**: Streamlined procedures for GPU issue resolution
