"""Public product release source and supported marketplace sources."""

PUBLIC_REPOSITORY = "https://github.com/MogooStudio/mogoo-codex-next.git"
RELEASE_REF = "main"
MARKETPLACE_REPOSITORY = "https://github.com/MogooStudio/mogoo-codex-plugin.git"
MARKETPLACE = "mogoo-codex-plugin"
MARKETPLACE_REF = "main"
PLUGIN = "mogoo-codex-next"
INSTALL_COMMAND = ("plugin", "add", PLUGIN + "@" + MARKETPLACE)
UPDATE_COMMANDS = (INSTALL_COMMAND,)
