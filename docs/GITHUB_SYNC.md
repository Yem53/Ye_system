# GitHub 自动同步指南

本项目已配置自动同步功能，可以将本地更改自动推送到 GitHub。

## 自动同步方式

### 方式 1: Git Post-Commit Hook（推荐）

每次执行 `git commit` 后，系统会自动尝试推送到 GitHub。

**使用方法：**
```bash
git add .
git commit -m "你的提交信息"
# 提交后会自动推送
```

### 方式 2: 使用自动同步脚本

#### Windows (PowerShell):
```powershell
.\scripts\auto_sync.ps1
# 或指定提交信息
.\scripts\auto_sync.ps1 "修复了滑动退出问题"
```

#### Linux/Mac:
```bash
chmod +x scripts/auto_sync.sh
./scripts/auto_sync.sh
# 或指定提交信息
./scripts/auto_sync.sh "修复了滑动退出问题"
```

## 身份验证设置

如果遇到身份验证错误，请使用以下方式之一：

### 方法 1: Personal Access Token (推荐)

1. 访问 GitHub: https://github.com/settings/tokens
2. 点击 "Generate new token" -> "Generate new token (classic)"
3. 设置权限：
   - 勾选 `repo` (完整仓库访问权限)
4. 生成并复制 Token
5. 推送时使用 Token 作为密码：
   ```bash
   git push origin main
   # Username: Yem53
   # Password: <粘贴你的 Token>
   ```

### 方法 2: SSH 密钥

1. 生成 SSH 密钥（如果还没有）：
   ```bash
   ssh-keygen -t ed25519 -C "yezfm53@gmail.com"
   ```

2. 将公钥添加到 GitHub:
   - 复制 `~/.ssh/id_ed25519.pub` 的内容
   - 访问 https://github.com/settings/keys
   - 点击 "New SSH key"，粘贴公钥

3. 更改远程仓库 URL 为 SSH:
   ```bash
   git remote set-url origin git@github.com:Yem53/Ye_system.git
   ```

### 方法 3: GitHub CLI

```bash
# 安装 GitHub CLI (如果还没有)
# Windows: winget install GitHub.cli
# Mac: brew install gh
# Linux: 根据发行版安装

# 登录
gh auth login

# 选择 GitHub.com -> HTTPS -> 登录方式
```

## 手动推送

如果自动同步失败，可以手动推送：

```bash
git add .
git commit -m "你的提交信息"
git push origin main
```

## 注意事项

- `.env` 文件已被 `.gitignore` 排除，不会上传到 GitHub
- 敏感信息（API Key、密码等）不会同步
- 如果推送失败，检查网络连接和身份验证设置

