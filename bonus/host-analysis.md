# Host Analysis - Service Cleanup

## Services to Remove

### High Priority
- **docker.service** - Security risk, unnecessary resource usage
- **lambda-jupyter.service** - Jupyter Lab, high security risk

### Medium Priority  
- **glances.service** - System monitoring (if not needed)


## Benefits
- Reduces attack surface
- Saves ~200-500MB memory
- Improves performance
- Reduces network exposure
- Reduces the confution about running containerd


