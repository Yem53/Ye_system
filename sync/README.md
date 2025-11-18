# Git 同步脚本使用指南

这个文件夹包含了用于同步本地代码和 GitHub 仓库的脚本。

## 脚本说明

### 1. `pull_from_github.ps1` / `pull_from_github.sh`
**从 GitHub 拉取最新代码到本地**

- 检查远程是否有更新
- 如果有更新，自动拉取并合并
- 如果本地有未提交的更改，会提示你先处理

**使用方法：**
```powershell
# Windows PowerShell
.\sync\pull_from_github.ps1
```

```bash
# Linux/Mac/Git Bash
chmod +x sync/pull_from_github.sh
./sync/pull_from_github.sh
```

### 2. `push_to_github.ps1` / `push_to_github.sh`
**推送本地更改到 GitHub**

- 检查本地是否有更改
- 自动添加、提交并推送到 GitHub

**使用方法：**
```powershell
# Windows PowerShell
.\sync\push_to_github.ps1
# 或指定提交信息
.\sync\push_to_github.ps1 "修复了滑动退出问题"
```

```bash
# Linux/Mac/Git Bash
chmod +x sync/push_to_github.sh
./sync/push_to_github.sh
# 或指定提交信息
./sync/push_to_github.sh "修复了滑动退出问题"
```

### 3. `sync_bidirectional.ps1` / `sync_bidirectional.sh` ⭐ **推荐**
**双向同步：先拉取远程更新，再推送本地更改**

- 先检查并拉取远程最新代码
- 然后检查并推送本地更改
- 自动处理冲突情况（暂存本地更改）

**使用方法：**
```powershell
# Windows PowerShell
.\sync\sync_bidirectional.ps1
# 或指定提交信息
.\sync\sync_bidirectional.ps1 "今天的修改"
```

```bash
# Linux/Mac/Git Bash
chmod +x sync/sync_bidirectional.sh
./sync/sync_bidirectional.sh
# 或指定提交信息
./sync/sync_bidirectional.sh "今天的修改"
```

## 使用场景

### 每天开始工作前
```powershell
.\sync\pull_from_github.ps1
```
确保本地代码是最新的。

### 工作结束后
```powershell
.\sync\push_to_github.ps1 "今天的修改"
```
将本地更改推送到 GitHub。

### 推荐：使用双向同步（一键完成）
```powershell
.\sync\sync_bidirectional.ps1 "今天的修改"
```
先拉取远程更新，再推送本地更改，确保代码同步。

## 注意事项

1. **身份验证**：如果遇到身份验证问题，需要配置 Personal Access Token 或 SSH 密钥
2. **冲突处理**：如果拉取时出现冲突，脚本会提示你手动解决
3. **未提交的更改**：如果本地有未提交的更改，拉取脚本会提示你先处理

## 故障排除

### 推送失败（身份验证问题）
1. 使用 Personal Access Token：
   ```powershell
   git push origin main
   # Username: Yem53
   # Password: <你的 Token>
   ```

2. 或使用 Token 直接推送：
   ```powershell
   git push https://YOUR_TOKEN@github.com/Yem53/Ye_system.git main
   ```

### 拉取失败（冲突）
1. 查看冲突文件：`git status`
2. 解决冲突后：`git add .` 然后 `git commit`
3. 或使用：`git pull --rebase origin main`

