# GPU Workload Debugging in Kubernetes

A comprehensive analysis and resolution of GPU workload failures in Kubernetes clusters, including automated diagnostic tools, security enhancements, and operational best practices.

## 🎯 Project Overview

This project addresses critical GPU workload failures in Kubernetes environments, focusing on container runtime misconfiguration, networking issues, and security vulnerabilities. The solution includes automated diagnostic tools, comprehensive documentation, and enhanced security configurations.

## 📁 Project Structure

```
bsr_assignment/
├── README.md                           # This file - Project overview
├── task1/                              # GPU Failure Diagnosis
│   ├── README.md                       # Detailed diagnosis analysis(slide 1 - Substitute)
│   └── scripts/
│       └── cluster_wide_gpu_debug.py   # Automated diagnostic tool
├── task2/                              # Resolution & fixes (slide 2 - Substitute)
│   ├── README.md                       # Complete resolution process
│   ├── nw-architecture.md              # Kubernetes networking analysis
│   ├── output.md                       # Task output documentation
│   └── scripts/
│       ├── cluster_wide_gpu_debug.py   # Enhanced diagnostic & fix tool
│       ├── debug.yaml                  # Network debugging workload
│       └── fixed_values.yaml           # GPU operator configuration fix
└── bonus/                              # Security & Enhancement Recommendations
    ├── enhancements.md                 # Cluster enhancement recommendations
    ├── gpu-workload-v2.yaml           # Secure GPU workload template
    ├── host-analysis.md               # Service cleanup recommendations
    └── nw-exposure.md                 # Network security analysis
```

## 🚀 Quick Navigation

- **[Task 1: Diagnosis](./task1/README.md)** - Root cause analysis of GPU workload failures
- **[Task 2: Resolution](./task2/README.md)** - Complete fix implementation and networking solutions
- **[Bonus: Enhancements](./bonus/)** - Security improvements and operational recommendations

## 🛠️ Key Tools & Scripts

### Automated Diagnostic Tool
**Location**: `task2/scripts/cluster_wide_gpu_debug.py`

**Usage**:
```bash
# Diagnosis only
python3 cluster_wide_gpu_debug.py

# Auto-fix with dry-run
python3 cluster_wide_gpu_debug.py --fix --dry-run

# Apply fixes
python3 cluster_wide_gpu_debug.py --fix
```

## 🚨 Critical Issues Resolved

1. **Container Runtime Misconfiguration**
   - Fixed dual containerd instance conflicts
   - Corrected NVIDIA runtime configuration paths
   - Updated GPU operator Helm values

2. **Networking Blockages**
   - Resolved DNS resolution failures
   - Configured firewall rules for Kubernetes traffic
   - Fixed external dependency access

3. **Security Vulnerabilities**
   - Identified exposed Kubernetes services
   - Recommended network policy implementations
   - Enhanced pod security configurations

## Workspace

On the host, config andn scripts contains some configurations/scripts used for 
task

root@192-18-132-31:~# ls
config  scripts 
