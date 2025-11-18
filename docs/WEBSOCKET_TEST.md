# WebSocket价格订阅服务测试指南

## 测试步骤

### 1. 安装依赖

确保已安装 `websocket-client`：

```bash
pip install websocket-client
```

或者使用项目的依赖管理：

```bash
pip install -e .
```

### 2. 启动服务

启动FastAPI服务：

```bash
python run_local.py
```

### 3. 检查启动日志

服务启动后，应该看到以下日志：

```
INFO: WebSocket价格订阅服务已启动
INFO: WebSocket价格订阅服务已启动，订阅 15 个交易对
```

如果看到错误，检查：
- 网络连接是否正常
- 代理配置是否正确（如果使用代理）
- `websocket-client` 是否已安装

### 4. 测试价格获取

#### 方法1：通过API测试

访问以下API端点测试价格获取：

```bash
# 获取单个交易对价格
curl http://localhost:8000/api/realtime/prices?symbols=BTCUSDT

# 获取多个交易对价格
curl http://localhost:8000/api/realtime/prices?symbols=BTCUSDT,ETHUSDT,BNBUSDT
```

#### 方法2：通过Dashboard测试

1. 打开浏览器访问 `http://localhost:8000`
2. 查看Dashboard中的持仓价格更新
3. 价格应该更新更快（延迟更低）

#### 方法3：使用测试脚本

运行测试脚本：

```bash
python test_websocket_price.py
```

测试脚本会：
1. 启动WebSocket价格订阅服务
2. 订阅BTC、ETH、BNB三个交易对
3. 等待价格数据更新
4. 显示实时价格
5. 监控价格变化

### 5. 验证WebSocket连接

检查日志中是否有价格更新：

```
DEBUG: 价格更新: BTCUSDT = 50000.00
DEBUG: 价格更新: ETHUSDT = 3000.00
```

### 6. 测试性能

#### 对比HTTP和WebSocket延迟

1. **HTTP方式**（禁用WebSocket）：
   - 在 `.env` 中设置 `WEBSOCKET_PRICE_ENABLED=false`
   - 重启服务
   - 观察价格获取延迟（通常50-200ms）

2. **WebSocket方式**（启用WebSocket）：
   - 在 `.env` 中设置 `WEBSOCKET_PRICE_ENABLED=true`
   - 重启服务
   - 观察价格获取延迟（通常0-10ms）

### 7. 测试自动重连

1. 断开网络连接
2. 观察日志中的重连尝试
3. 恢复网络连接
4. 验证服务自动恢复

### 8. 测试动态订阅

当执行交易时，系统会自动订阅新的交易对。检查日志：

```
INFO: 动态订阅交易对: NEWCOINUSDT
```

## 常见问题

### 问题1：WebSocket连接失败

**症状**：日志中显示连接错误

**解决方案**：
1. 检查网络连接
2. 检查代理配置（如果使用代理）
3. 确认币安WebSocket服务可访问

### 问题2：价格数据不更新

**症状**：价格缓存一直为空

**解决方案**：
1. 检查订阅的交易对是否正确
2. 等待更长时间（可能需要几秒建立连接）
3. 检查日志中的错误信息

### 问题3：依赖未安装

**症状**：`ModuleNotFoundError: No module named 'websocket'`

**解决方案**：
```bash
pip install websocket-client
```

## 性能指标

### 预期性能

- **价格获取延迟**：0-10ms（WebSocket缓存）
- **价格更新频率**：实时推送（币安推送频率）
- **连接建立时间**：< 1秒
- **内存占用**：每个交易对约几KB

### 对比HTTP API

| 指标 | HTTP API | WebSocket |
|------|----------|-----------|
| 延迟 | 50-200ms | 0-10ms |
| 更新方式 | 轮询 | 推送 |
| API调用次数 | 每次请求 | 仅连接时 |
| 实时性 | 延迟 | 实时 |

## 配置选项

### 环境变量

在 `.env` 文件中可以配置：

```env
# 启用/禁用WebSocket价格订阅（默认：true）
WEBSOCKET_PRICE_ENABLED=true

# 自定义订阅的交易对列表（可选）
# 如果未设置，使用默认的15个常见交易对
WEBSOCKET_PRICE_SYMBOLS=BTCUSDT,ETHUSDT,SOLUSDT
```

## 监控建议

1. **日志监控**：关注WebSocket连接和价格更新的日志
2. **性能监控**：监控价格获取的延迟
3. **错误监控**：关注连接失败和重连的日志
4. **缓存监控**：检查价格缓存的有效性

