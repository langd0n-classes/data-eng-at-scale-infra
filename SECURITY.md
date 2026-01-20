# Security Policy

## Secrets Management

**IMPORTANT:** This repository contains infrastructure templates only. **No secrets, passwords, or credentials are stored in this repository.**

### What to Avoid

- Never commit real passwords or tokens
- Never commit kubeconfig files
- Never commit TLS certificates or private keys
- Never commit API keys or registry credentials
- Never commit internal hostnames or IP addresses

### Handling Credentials

All credential fields in manifests use placeholders:

```yaml
# Example: NiFi password (line from template)
- name: SINGLE_USER_CREDENTIALS_PASSWORD
  value: "${TEAM_PASSWORD}"  # ← Substitute at deploy time
```

**Best practices:**

1. **Environment Variables** - Pass secrets via environment at deploy time
2. **Kubernetes Secrets** - Create Secret objects separately
3. **External Secret Managers** - Use Vault, AWS Secrets Manager, etc.
4. **Sealed Secrets** - For GitOps workflows, use SealedSecrets or SOPS

### Example: Deploying with Secrets

```bash
# Set password as environment variable
export TEAM_PASSWORD="$(openssl rand -base64 32)"

# Deploy with substitution
envsubst < nifi/team-statefulset-template.yaml | kubectl apply -f -
```

### Network Security

The provided manifests use:

- **PLAINTEXT** Kafka listeners for simplicity
- **HTTP** for some services
- Minimal RBAC and network policies

For production use, you should add:

1. **TLS/SSL** - Encrypt all traffic
2. **Network Policies** - Restrict pod-to-pod communication
3. **RBAC** - Implement least-privilege access
4. **Pod Security Standards** - Enforce security contexts
5. **Image Scanning** - Scan container images for vulnerabilities

### OpenShift Security Context Constraints (SCC)

NiFi requires elevated privileges to run:

```bash
# Grant anyuid SCC (required for NiFi)
oc adm policy add-scc-to-user anyuid -z default -n ${NAMESPACE}
```

**Note:** Only grant this in trusted namespaces. For production, create a custom SCC with minimal required privileges.

### Reporting Security Issues

If you discover a security vulnerability:

1. **Do not** open a public issue
2. Contact the maintainers privately
3. Provide details and reproduction steps
4. Allow time for a fix before public disclosure

## Compliance Considerations

This infrastructure is designed for:

- Educational use
- Development environments
- Demonstrations and POCs

For production or regulated environments, additional hardening is required:

- Implement authentication and authorization
- Enable audit logging
- Encrypt data at rest and in transit
- Apply security scanning and policies
- Conduct regular security reviews

## Security Checklist for Deployment

Before deploying:

- [ ] All secrets managed externally (not in Git)
- [ ] TLS enabled for external access
- [ ] RBAC configured with minimal permissions
- [ ] Network policies applied
- [ ] Storage encrypted if required
- [ ] Pod security contexts configured
- [ ] Image pull policies set correctly
- [ ] Resource limits defined
- [ ] Monitoring and alerting configured

## Updates and Patches

- Monitor CVEs for Kafka, NiFi, and base images
- Update container images regularly
- Test updates in non-production first
- Review Kubernetes/OpenShift security advisories

## Further Reading

- [Kubernetes Security Best Practices](https://kubernetes.io/docs/concepts/security/)
- [OpenShift Security Guide](https://docs.openshift.com/container-platform/latest/security/)
- [Apache Kafka Security](https://kafka.apache.org/documentation/#security)
- [Apache NiFi Security](https://nifi.apache.org/docs/nifi-docs/html/administration-guide.html#security-configuration)
