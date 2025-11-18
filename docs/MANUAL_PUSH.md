# 手动推送指南

如果自动推送遇到问题，可以按照以下步骤手动推送：

## 方法 1: 使用 Token 直接推送（推荐）

1. 确保远程仓库已配置：
   ```bash
   git remote -v
   # 应该显示: origin  https://github.com/Yem53/Ye_system.git
   ```

2. 使用 Token 推送：
   ```bash
   git push https://YOUR_TOKEN@github.com/Yem53/Ye_system.git main
   ```
   注意：将 `YOUR_TOKEN` 替换为你的 Personal Access Token

## 方法 2: 配置 Git Credential Helper

### Windows (使用 Windows Credential Manager):

```powershell
# 配置使用 Windows Credential Manager
git config --global credential.helper manager-core

# 推送时会提示输入用户名和密码
# Username: Yem53
# Password: <你的 Personal Access Token>
git push origin main
```

### 或者使用 Git Credential Store:

```bash
# 配置使用文件存储
git config --global credential.helper store

# 推送一次后，凭据会保存
git push origin main
# Username: Yem53
# Password: <你的 Personal Access Token>
```

## 方法 3: 使用 GitHub CLI

```bash
# 安装 GitHub CLI (如果还没有)
# Windows: winget install GitHub.cli

# 使用 token 登录
gh auth login --with-token <<< "YOUR_TOKEN"

# 推送
git push origin main
```

## 验证 Token 是否有效

访问以下 URL（在浏览器中）验证 token 是否有效：
```
https://api.github.com/user
```

在浏览器中打开时，会提示输入用户名和密码：
- Username: `Yem53`
- Password: `<你的 Personal Access Token>`

如果返回你的用户信息，说明 token 有效。

## 当前待推送的提交

运行以下命令查看待推送的提交：
```bash
git log origin/main..main --oneline
```

## 故障排除

如果遇到 500/503 错误：
1. 等待几分钟后重试（可能是 GitHub 服务器临时问题）
2. 检查网络连接
3. 验证 token 是否还有效（token 可能已过期或被撤销）
4. 检查仓库权限（确保 token 有 `repo` 权限）

## 安全提示

⚠️ **重要**: Token 已保存在此文档中，请确保：
- 不要将包含 token 的文件提交到公开仓库
- 如果 token 泄露，立即在 GitHub 设置中撤销并生成新 token
- 定期轮换 token

