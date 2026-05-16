"""NASVault MCP server — Claude Desktop / Code 直接管 NAS。

通过 HTTP 代理到本机 Flask app。这样所有契约 #1 destructive 双段式、metadata
provider 守门、validate_path 校验都复用，零代码重复。

ROADMAP #7：7 个 tool wrap 现有 REST：
  list_files / find_recent_downloads / get_disk_usage
  find_duplicates / list_archive_candidates
  prepare_destructive_action / confirm_destructive_action

安全模式：默认 stdio + 调本机 127.0.0.1，远程访问推 Tailscale。
"""
