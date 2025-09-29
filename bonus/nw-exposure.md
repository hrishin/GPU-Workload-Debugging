# Kubernetes Service Access Analysis

## Current Service Exposure

```bash
root@192-18-132-31:~# netstat -ntulp | grep -i kube
tcp        0      0 127.0.0.1:10248         0.0.0.0:*               LISTEN      544673/kubelet      
tcp        0      0 127.0.0.1:10249         0.0.0.0:*               LISTEN      402515/kube-proxy   
tcp        0      0 127.0.0.1:10256         0.0.0.0:*               LISTEN      402515/kube-proxy   
tcp6       0      0 :::6443                 :::*                    LISTEN      513163/kube-apiserv 
tcp6       0      0 :::10257                :::*                    LISTEN      402503/kube-control 
tcp6       0      0 :::10259                :::*                    LISTEN      402527/kube-schedul 
tcp6       0      0 :::10250                :::*                    LISTEN      544673/kubelet      
```

## Security Risk Assessment

### High Risk - Exposed to Internet
- **API Server (6443)**: `:::6443` - Critical security risk
- **Controller Manager (10257)**: `:::10257` - Should be localhost only
- **Scheduler (10259)**: `:::10259` - Should be localhost only  
- **Kubelet (10250)**: `:::10250` - Should be internal network only

### Low Risk - Localhost Only
- **Kubelet Health (10248)**: `127.0.0.1:10248` ✓
- **Kube-proxy Metrics (10249)**: `127.0.0.1:10249` ✓
- **Kube-proxy Health (10256)**: `127.0.0.1:10256` ✓

## Immediate Security Fixes

```bash
# Block external access to control plane
ufw deny 6443   # API Server
ufw deny 10257  # Controller Manager  
ufw deny 10259  # Scheduler
ufw deny 10250  # Kubelet

# Allow only internal networks
ufw allow from 10.0.0.0/8 to any port 6443
ufw allow from 192.168.0.0/16 to any port 6443
```

## Configuration Updates

```bash
# Bind control plane components to localhost
--bind-address=127.0.0.1  # For controller-manager and scheduler
--bind-address=192.168.1.100  # For API server (internal IP)
```

## Cilium Network Policies (Recommended)

Instead of UFW, use Cilium network policies for better Kubernetes-native security:

### Deny All Ingress Traffic
```yaml
apiVersion: cilium.io/v2
kind: CiliumNetworkPolicy
metadata:
  name: deny-all-ingress
  namespace: kube-system
spec:
  endpointSelector: {}
  ingress:
  - {}
```

### Allow API Server Access Only from Internal Networks
```yaml
apiVersion: cilium.io/v2
kind: CiliumNetworkPolicy
metadata:
  name: allow-apiserver-internal
  namespace: kube-system
spec:
  endpointSelector:
    matchLabels:
      k8s:app: kube-apiserver
  ingress:
  - fromCIDR:
    - 10.0.0.0/8
    - 192.168.0.0/16
    - 172.16.0.0/12
    toPorts:
    - ports:
      - port: "6443"
        protocol: TCP
```

### Restrict Control Plane Components to Localhost
```yaml
apiVersion: cilium.io/v2
kind: CiliumNetworkPolicy
metadata:
  name: restrict-control-plane-localhost
  namespace: kube-system
spec:
  endpointSelector:
    matchLabels:
      k8s:app: kube-controller-manager
  ingress:
  - fromCIDR:
    - 127.0.0.1/32
    toPorts:
    - ports:
      - port: "10257"
        protocol: TCP
---
apiVersion: cilium.io/v2
kind: CiliumNetworkPolicy
metadata:
  name: restrict-scheduler-localhost
  namespace: kube-system
spec:
  endpointSelector:
    matchLabels:
      k8s:app: kube-scheduler
  ingress:
  - fromCIDR:
    - 127.0.0.1/32
    toPorts:
    - ports:
      - port: "10259"
        protocol: TCP
```

### Apply Policies
```bash
# Apply all network policies
kubectl apply -f cilium-network-policies.yaml

# Verify policies are active
kubectl get cnp -n kube-system
```

## Benefits of Cilium over UFW

1. **Kubernetes Native**: Policies are managed as Kubernetes resources
2. **Granular Control**: Per-pod, per-namespace, per-label based rules
3. **Dynamic**: Policies update automatically with pod changes
4. **Observability**: Built-in monitoring and logging
5. **eBPF Performance**: Faster than iptables-based solutions

## Impact
- GPU workloads unaffected (run in internal pod network)
- Only affects external access to control plane
- Improves cluster security posture significantly
- Better integration with Kubernetes ecosystem

