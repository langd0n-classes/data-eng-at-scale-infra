from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    slack_signing_secret: str          # required — fails at startup if missing
    infra_namespace: str = "infra"
    event_generator_name: str = "event-generator"
    chatops_name: str = "slack-chatops"  # used in status-all + wipe protection
    admin_channel_id: str = ""         # empty = admin commands disabled from all channels

    # Team registry — cluster-side source of truth for team name → namespace mappings
    team_registry_name: str = "team-registry"
    team_passwords_name: str = "team-passwords"

    # Cluster-specific values needed for direct resource creation (add-kafka / add-nifi)
    storage_class: str = "standard"
    nifi_image: str = ""               # e.g. quay.io/langdon/nifi-openshift:latest
    external_domain: str = ""          # e.g. apps.your-cluster.example.com

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
