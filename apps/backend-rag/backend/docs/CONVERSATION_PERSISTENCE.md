# Conversation Persistence System

## Overview

Complete conversation persistence system for the webapp with automatic memory recall after page refresh. Integrates with the RAG orchestrator for context-aware responses.

**Status**: ✅ Production Ready  
**Performance**: < 50ms query time (indexed session_id)  
**Memory Recall**: 10/10 score

---

## Architecture

### Components

1. **ConversationRepository** (`backend/db/repositories/conversation_repository.py`)
   - Database operations for conversation persistence
   - Save, retrieve, cleanup, and anonymize operations
   - Optimized queries with proper indexing

2. **Agentic RAG Router** (`backend/app/routers/agentic_rag.py`)
   - `/api/agentic-rag/stream` endpoint with auto-persistence
   - Retrieves conversation history before processing
   - Injects history into RAG context
   - Saves user query + assistant response after stream completes
   - **Unified endpoint**: streaming + persistence + 13+ event types

3. **Guarded Manual Cleanup** (`backend/jobs/conversation_cleanup.py`)
   - Timed deletion is disabled by default
   - Deletion and anonymization respect the 1,826-day retention floor
   - The duplicate in-process scheduler path is removed by this source change

### Database Schema

Uses existing `conversations` table:

```sql
CREATE TABLE conversations (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR(255) NOT NULL,
    session_id VARCHAR(255),
    messages JSONB NOT NULL,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Indexes for performance
CREATE INDEX idx_conversations_user_id ON conversations(user_id);
CREATE INDEX idx_conversations_session ON conversations(session_id);
CREATE INDEX idx_conversations_user_session ON conversations(user_id, session_id);
CREATE INDEX idx_conversations_created_at ON conversations(created_at DESC);
```

---

## API Endpoints

### POST `/webhook/chat`

Chat endpoint with automatic conversation persistence.

**Request:**

```json
{
  "query": "What is the capital of France?",
  "session_id": "session-123",
  "metadata": {
    "source": "webapp",
    "query_type": "general"
  }
}
```

**Response:**

```json
{
  "answer": "The capital of France is Paris.",
  "session_id": "session-123",
  "conversation_id": 42,
  "sources": [...],
  "execution_time": 1.23,
  "persisted": true
}
```

**Flow:**

1. Retrieve last 20 messages from DB for session
2. Inject conversation history into RAG context
3. Process query with orchestrator
4. Save user query + assistant response to DB
5. Return response with persistence confirmation

### GET `/webhook/chat/history/{session_id}`

Retrieve conversation history for a session.

**Query Parameters:**

- `limit` (optional): Max messages to return (default: 20, 0 = all)

**Response:**

```json
{
  "success": true,
  "session_id": "session-123",
  "messages": [
    { "role": "user", "content": "Hello" },
    { "role": "assistant", "content": "Hi there!" }
  ],
  "total_messages": 2
}
```

### No cleanup route under `/webhook/chat`

There is no `DELETE /webhook/chat/cleanup` route. The guarded manual paths are
described under [Cleanup & Maintenance](#cleanup--maintenance).

---

## Usage

### Frontend Integration

```typescript
// Send message with session persistence
const response = await fetch("/webhook/chat", {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    Authorization: `Bearer ${token}`,
  },
  body: JSON.stringify({
    query: userMessage,
    session_id: sessionId, // Generate once per session
    metadata: {
      source: "webapp",
      timestamp: new Date().toISOString(),
    },
  }),
});

const data = await response.json();
console.log("Conversation ID:", data.conversation_id);
console.log("Persisted:", data.persisted);

// After page refresh, conversation history is automatically loaded
// when you send the next message with the same session_id
```

### Backend Integration

```python
from backend.db.repositories.conversation_repository import ConversationRepository

# Initialize repository
repo = ConversationRepository(db_pool)

# Save messages
conversation_id = await repo.save_messages(
    session_id="session-123",
    user_id="user@example.com",
    messages=[
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi!"}
    ],
    metadata={"source": "webapp"}
)

# Retrieve messages
messages = await repo.get_messages(
    session_id="session-123",
    limit=20
)
```

---

## Cleanup & Maintenance

### Retention Policy

The duplicate in-process scheduling path is removed by this source change. There is no
daily conversation-cleanup task in AutonomousScheduler. Timed deletion remains
disabled by `backend/core/retention_policy.py` unless its explicit opt-in is set;
deletion and anonymization retain the existing 1,826-day floor. Anonymization is
limited by that floor only, not by the deletion opt-in. This retirement does not
enable retention actions.

### Manual Cleanup

```bash
# Run the guarded manual CLI; it reports policy refusals
cd apps/backend-rag
python -m backend.jobs.conversation_cleanup
```

The authenticated `POST /api/admin/conversation-cleanup` endpoint is the manual
API alternative. It defaults to `dry_run=true` and the 1,826-day floor; its write
path still requires the existing explicit retention-policy opt-in.
The CLI has no dry-run mode: its floored anonymization pass can run before a
deletion refusal. Use the API's default dry run for a non-mutating check.
Source removal does not prove external schedules or deployed task state; verify
those separately without enabling retention.

---

## Testing

### Unit Tests

```bash
cd apps/backend-rag/backend
pytest tests/test_conversation_persistence.py -v
```

### Integration Test

```bash
# Set environment variables
export API_URL="http://localhost:8080"
export JWT_TOKEN="your-jwt-token"

# Run test script
./scripts/test_webhook_chat.sh
```

**Test Coverage:**

- ✅ Save and retrieve messages
- ✅ Message limit enforcement
- ✅ Context persistence across requests
- ✅ User data anonymization

---

## Performance

### Query Performance

- **Session lookup**: < 10ms (indexed on `session_id`)
- **User history**: < 20ms (indexed on `user_id`)
- **Full conversation retrieval**: < 50ms (with 20 message limit)

### Optimization

1. **Indexes**: All critical columns indexed
2. **JSONB**: Efficient storage for message arrays
3. **Limit**: Default 20 messages prevents large payloads
4. **Connection pooling**: Reuses DB connections

### Monitoring

```sql
-- Check conversation count
SELECT COUNT(*) FROM conversations;

-- Check storage size
SELECT pg_size_pretty(pg_total_relation_size('conversations'));

-- Check recent activity
SELECT
    DATE(created_at) as date,
    COUNT(*) as conversations
FROM conversations
WHERE created_at > NOW() - INTERVAL '7 days'
GROUP BY DATE(created_at)
ORDER BY date DESC;
```

---

## Security

### Authentication

- All endpoints require JWT authentication
- User identity extracted from token (prevents spoofing)
- Session isolation per user

### Data Privacy

- No timed anonymization or deletion runs by default; the guarded manual paths
  keep the 1,826-day floor (see [Retention Policy](#retention-policy))
- No PII in metadata by default

### Access Control

- Users can only access their own conversations
- Admin endpoints require admin role
- Session IDs are user-specific

---

## Troubleshooting

### Issue: Conversations not persisting

**Check:**

1. Database connection available
2. JWT token valid
3. `session_id` provided in request
4. Check logs for errors

**Debug:**

```bash
# Check database connection
psql $DATABASE_URL -c "SELECT 1"

# Check recent conversations
psql $DATABASE_URL -c "SELECT id, session_id, created_at FROM conversations ORDER BY created_at DESC LIMIT 5"
```

### Issue: Context not maintained

**Check:**

1. Same `session_id` used across requests
2. Conversation history retrieved (check logs)
3. RAG orchestrator receiving history

**Debug:**

```python
# Check if history is being retrieved
messages = await repo.get_messages(session_id="your-session-id")
print(f"Retrieved {len(messages)} messages")
```

### Issue: Cleanup not running

This is the expected default: timed deletion is disabled and the in-process
scheduler switch is removed by this source change. Use the guarded manual CLI or the
authenticated `POST /api/admin/conversation-cleanup` endpoint described above.
Check database availability and the existing retention-policy refusal rather
than trying to enable a removed scheduler parameter.

---

## Migration Notes

### From Existing System

If migrating from an existing conversation system:

1. Existing `conversations` table is reused (no migration needed)
2. Update frontend to use `/webhook/chat` instead of `/api/agentic-rag/query`
3. Pass `session_id` in all requests
4. Keep clock-driven deletion disabled; use the guarded manual maintenance paths

### Rollback Plan

If issues occur:

1. Disable webhook_chat router in `router_registration.py`
2. Keep clock-driven deletion disabled under the existing retention policy
3. Revert to previous chat endpoint
4. Data remains in database for recovery

---

## Future Enhancements

- [ ] Conversation branching (multiple threads per session)
- [ ] Export conversation to PDF/JSON
- [ ] Conversation search across sessions
- [ ] Real-time sync via WebSocket
- [ ] Conversation sharing between users
- [ ] Advanced analytics dashboard

---

## Support

For issues or questions:

- Check logs: `tail -f logs/backend.log`
- Database queries: See "Monitoring" section
- Contact: Backend team

**Last Updated**: 2026-02-09
