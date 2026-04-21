# WhatsApp CRM 系统架构升级与稳定性修复

## 变更概述

本次变更对 WhatsApp CRM 系统进行了重大架构升级，引入适配器模式统一不同后端（Neonize/CLI）的管理，同时修复了 Neonize 后端的多项稳定性问题。核心目标包括：实现后端可插拔切换、提升消息收发可靠性、解决 Session 管理问题、增强系统可维护性。

---

## 架构变更

### 1. 适配器模式引入

新增统一接口层，实现不同 WhatsApp 后端的无缝切换：

- **`IWhatsAppClient` 接口**（`whatsapp_interface.py`）：定义所有后端必须实现的标准方法
  - `connect()` / `disconnect()` - 连接管理
  - `send_message()` - 消息发送
  - `on_message()` - 消息接收注册
  - `is_connected()` / `is_authenticated()` - 状态检查
  - `get_qr_code()` - 二维码获取

- **`WhatsAppClientManager`**（`whatsapp_adapter.py`）：统一管理器
  - 通过环境变量 `WHATSAPP_BACKEND` 自动选择后端
  - 封装不同后端的启动逻辑差异（Neonize 线程启动 vs CLI 子进程）
  - 提供统一的生命周期管理（初始化、关闭、消息处理器注册）

### 2. 后端切换机制

```python
# 通过环境变量切换后端
WHATSAPP_BACKEND=neonize  # 使用 Neonize 后端（默认）
WHATSAPP_BACKEND=cli      # 使用 CLI 后端
```

---

## 稳定性修复

### Neonize 后端（`neonize_client.py`）

| 修复项 | 问题描述 | 解决方案 |
|--------|----------|----------|
| **消息队列机制** | 消息处理阻塞主线程，导致消息丢失 | 引入 `queue.Queue` 异步队列，后台线程消费处理 |
| **消息去重机制** | 重复消息多次触发，造成重复回复 | 使用 `OrderedDict` 缓存消息 ID，30分钟 TTL 自动清理 |
| **心跳检测机制** | 连接僵死无法感知，消息发送失败无告警 | 5分钟无消息告警 + 连续发送失败3次触发重连 |
| **发送超时保护** | 发送消息无限阻塞 | 添加 15秒超时 + 2次重试机制 |
| **连接重连机制** | 断开后无法自动恢复 | 指数退避重试（最多5次，最大间隔30秒） |
| **队列溢出保护** | 消息队列满时新消息丢失 | 队列大小限制5000，80%利用率告警，满队列时有限重试 |
| **内存泄漏防护** | 消息 ID 缓存无限增长 | 最大缓存10000条，超限清理至50% |

### Communication Service（`communication_service.py`）

- **Session 管理修复**：使用上下文管理器 `_get_db()` 统一管理数据库会话，避免 ORM 对象脱管问题
- **转人工请求处理**：修复会话状态更新时的 Session 绑定问题
- **消息发送接口**：统一使用客户端的 `send_message()`，内部处理 JID 格式转换

### LLM Service（`llm_service.py`）

- **新增 `_get_customer_attr()` 辅助函数**：统一处理 ORM 对象和 dict 类型的客户属性访问，解决多线程环境下 Session 失效导致的属性读取错误

### Main 入口（`main.py`）

- **日志配置优化**：统一日志级别配置（`LOG_LEVEL` 环境变量），输出到文件和控制台
- **初始化简化**：使用 `create_client_manager()` 统一初始化，自动根据环境变量选择后端
- **生命周期管理**：使用 `lifespan` 上下文管理器统一处理启动和关闭流程

---

## 修改文件清单

| 文件 | 变更说明 |
|------|----------|
| `backend/whatsapp_interface.py` | **新增** - 定义 `IWhatsAppClient` 统一接口 |
| `backend/whatsapp_adapter.py` | **重写** - 实现 `WhatsAppClientManager` 适配器管理器 |
| `backend/neonize_client.py` | **修改** - 添加队列、去重、心跳、重连等稳定性机制 |
| `backend/communication_service.py` | **修改** - 修复 Session 管理，统一消息发送接口 |
| `backend/llm_service.py` | **修改** - 新增 `_get_customer_attr()` 辅助函数 |
| `backend/main.py` | **修改** - 日志配置、初始化简化、生命周期管理 |

---

## 新增文件

| 文件 | 用途 |
|------|------|
| `backend/whatsapp_interface.py` | 定义 `IWhatsAppClient` 抽象接口，所有后端必须实现 |
| `backend/whatsapp_adapter.py` | 统一管理器，通过环境变量切换后端，封装生命周期管理 |

---

## 配置变更

### 新增环境变量

| 变量名 | 说明 | 默认值 |
|--------|------|--------|
| `WHATSAPP_BACKEND` | 选择 WhatsApp 后端 | `neonize` |
| `LOG_LEVEL` | 日志级别 | `INFO` |

### 使用方式

```bash
# 使用 Neonize 后端（推荐，默认）
export WHATSAPP_BACKEND=neonize
python backend/main.py

# 使用 CLI 后端
export WHATSAPP_BACKEND=cli
python backend/main.py
```

---

## 备份说明

原始文件备份位置：`backend/backup/neonize_original/`

| 备份文件 | 原文件 |
|----------|--------|
| `main_original.py` | `main.py` |
| `neonize_client_original.py` | `neonize_client.py` |
| `whatsapp_adapter_original.py` | `whatsapp_adapter.py` |
| `whatsapp_client_original.py` | `whatsapp_client.py` |

如需回滚，可从备份目录恢复原始文件。

---

## 方案调研结论

### 备选方案评估

| 方案 | 评分 | 结论 |
|------|------|------|
| **Baileys** (Node.js) | 45/100 | 技术栈不匹配（Node.js vs Python），迁移成本高，API 差异大，暂不考虑 |
| **RPA 方案** (Puppeteer/Playwright) | 32/100 | 依赖 Web WhatsApp，稳定性差，易被封禁，维护成本高，不推荐 |
| **Neonize** (Python) | **推荐** | 原生 Python，与现有架构兼容，维护成本最低，当前已修复稳定性问题 |

### 推荐路径

继续使用 **Neonize** 作为主力后端，理由：

1. **技术栈一致**：Python 生态，与现有代码无缝集成
2. **架构兼容**：通过适配器模式可平滑切换后端
3. **稳定性已解决**：本次修复已解决主要稳定性问题（队列、去重、心跳、重连）
4. **维护成本低**：无需引入额外的 Node.js 依赖或浏览器自动化
5. **可扩展性**：后续如需切换，只需实现 `IWhatsAppClient` 接口即可

---

## 验证建议

1. **功能验证**：消息收发、二维码登录、自动回复、转人工
2. **稳定性验证**：长时间运行（24小时+），观察消息队列和连接状态
3. **切换验证**：测试 `WHATSAPP_BACKEND=cli` 回退功能
4. **监控检查**：查看 `/tmp/whatsapp_crm.log` 日志，确认无异常报错

---

*文档版本：v1.0*  
*更新日期：2026-04-19*
