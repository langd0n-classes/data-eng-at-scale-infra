# Contributing

This repository supports classroom deployments of per-team Kafka + NiFi with a shared event generator. Contributions are welcome from instructors, students, and collaborators. Please keep changes focused on improving infrastructure, documentation, and student experience.

## Scope

- Infrastructure manifests (Kafka, NiFi, storage)
- Event generator improvements and docs
- Classroom workflows and setup instructions
- Troubleshooting and operational guidance

Out of scope:
- Assignment solutions or grading logic
- Real secrets, kubeconfigs, tokens
- Internal hostnames or private URLs

## Reporting Issues

When reporting a problem:
- Provide clear reproduction steps and expected vs actual behavior
- Include environment details (Kubernetes/OpenShift version, storage class)
- Share logs and relevant YAML (redacted if needed)
- Note whether the issue affects per-team or shared deployments

## Suggesting Enhancements

For feature requests:
- Explain the classroom use case and benefit
- Consider backward compatibility and deployment simplicity
- Propose a minimal implementation approach
- Include example configuration where relevant

## Submitting Changes

1. Fork the repository
2. Create a topic branch: `feature/<short-name>` or `fix/<short-name>`
3. Make focused changes with clear commit messages
4. Test on a Kubernetes/OpenShift cluster (per-team and/or shared modes)
5. Update related docs (README, examples)
6. Open a PR describing the change and classroom impact

## Development Guidelines

### Testing Changes
- Verify deploy and cleanup across namespaces
- Test with different storage classes
- Confirm event generator produces mixed events and downstream splitting works
- Use `envsubst` for templates and confirm variable coverage

### Code Style
- Consistent YAML indentation (2 spaces)
- Comments for non-obvious configurations
- Modular, reusable manifests
- Document new environment variables

### Documentation
- Keep docs student-friendly and neutral
- Include example commands and prerequisites
- Avoid instructor-only prose or internal details

## Student Contributions

Students may propose:
- Clarifications to setup instructions
- Bug fixes in manifests or scripts
- Minor improvements to event generator docs

Please avoid:
- Including assignment solutions or rubrics
- Adding real credentials or internal URLs

## Community Guidelines

- Be respectful and constructive
- Provide context and rationale
- Prefer small, reviewable changes
- Help others via issues and discussions

## Security and Anti-Leak

Before submitting a PR:
- Ensure no secrets or tokens are included
- Use placeholder hostnames and example values
- Keep student/instructor identifiers out of public docs

Thank you for contributing!
