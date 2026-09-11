#!/bin/bash

SESSION="bob"
API_KEY="bob_prod_bob-apikey_5Ap754RkPFcTQeVFxsmoYTp4C8ZjtyW3tru8yTUE4KGe6zsnVe5ZhTpowAxrsnLjz6psvKogXn5ZzwkejVgeshDt_Fi2Fxt58GvA9yj86fsSJ6VzoZc8HmEpaBbttUQPx6uSB"

# Check if session already running
if tmux has-session -t $SESSION 2>/dev/null; then
  echo "⚠️  Bob is already running in tmux session: "
  echo "    Attach with: tmux attach -t $SESSION"
  exit 0
fi

# Start Bob in a new detached tmux session
tmux new-session -d -s $SESSION -x 220 -y 50 \
  "export BOB_API_KEY=$API_KEY; bob chat --accept-license 2>&1 | tee /tmp/bob.log"

sleep 2

if tmux has-session -t $SESSION 2>/dev/null; then
  echo "✅ Bob is running in tmux session: "
else
  echo "❌ Bob failed to start. Check logs: cat /tmp/bob.log"
  exit 1
fi

echo ""
echo "Commands:"
echo "  Attach   → tmux attach -t $SESSION"
echo "  Detach   → Ctrl+B, then D"
echo "  Kill     → tmux kill-session -t $SESSION"
echo "  Restart  → ~/start-bob.sh"
echo "  Logs     → cat /tmp/bob.log"
