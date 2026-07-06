import { useCallback, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import { WEBSOCKET_URL } from "./config.js";
import "./App.css";

const RECONNECT_DELAY_MS = 2000;

function PlateGlyph({ size = 18 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <circle cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="2" />
      <circle cx="12" cy="12" r="3.5" fill="currentColor" />
    </svg>
  );
}

function useChatSocket() {
  const [status, setStatus] = useState("connecting"); // connecting | connected | error
  const [messages, setMessages] = useState([]);
  const [pending, setPending] = useState(false);
  const socketRef = useRef(null);
  const reconnectTimerRef = useRef(null);

  const connect = useCallback(() => {
    setStatus("connecting");
    const socket = new WebSocket(WEBSOCKET_URL);
    socketRef.current = socket;

    socket.onopen = () => setStatus("connected");

    socket.onmessage = (event) => {
      let payload;
      try {
        payload = JSON.parse(event.data);
      } catch {
        return;
      }
      setPending(false);
      if (payload.type === "chat_token") {
        setMessages((prev) => [...prev, { role: "assistant", content: payload.content }]);
      } else if (payload.type === "error") {
        setMessages((prev) => [...prev, { role: "system", content: payload.content }]);
      }
    };

    socket.onclose = () => {
      setStatus("error");
      reconnectTimerRef.current = setTimeout(connect, RECONNECT_DELAY_MS);
    };

    socket.onerror = () => {
      socket.close();
    };
  }, []);

  useEffect(() => {
    connect();
    return () => {
      clearTimeout(reconnectTimerRef.current);
      socketRef.current?.close();
    };
  }, [connect]);

  const sendPrompt = useCallback((prompt) => {
    if (socketRef.current?.readyState !== WebSocket.OPEN) return;
    setMessages((prev) => [...prev, { role: "user", content: prompt }]);
    setPending(true);
    socketRef.current.send(JSON.stringify({ prompt }));
  }, []);

  return { status, messages, pending, sendPrompt };
}

function Message({ role, content }) {
  if (role === "user") {
    return (
      <div className="message message-user">
        <div className="bubble bubble-user">{content}</div>
      </div>
    );
  }
  if (role === "system") {
    return (
      <div className="message message-system">
        <div className="bubble bubble-system">{content}</div>
      </div>
    );
  }
  return (
    <div className="message message-assistant">
      <div className="bubble bubble-assistant">
        <ReactMarkdown>{content}</ReactMarkdown>
      </div>
    </div>
  );
}

function ThinkingIndicator() {
  return (
    <div className="message message-assistant">
      <div className="bubble bubble-assistant thinking" aria-label="Coach is thinking">
        <span className="dot" />
        <span className="dot" />
        <span className="dot" />
      </div>
    </div>
  );
}

export default function App() {
  const { status, messages, pending, sendPrompt } = useChatSocket();
  const [draft, setDraft] = useState("");
  const listRef = useRef(null);

  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, pending]);

  const handleSubmit = (event) => {
    event.preventDefault();
    const prompt = draft.trim();
    if (!prompt || status !== "connected") return;
    sendPrompt(prompt);
    setDraft("");
  };

  return (
    <div className="app">
      <header className="header">
        <span className="header-title">
          <PlateGlyph />
          Workout Coach
        </span>
        <span className={`status-dot status-${status}`} title={status} />
      </header>

      <main className="messages" ref={listRef}>
        {messages.length === 0 && (
          <p className="empty-state">Ask about your training — strength, volume, or where you might be stalling.</p>
        )}
        {messages.map((m, i) => (
          <Message key={i} role={m.role} content={m.content} />
        ))}
        {pending && <ThinkingIndicator />}
      </main>

      <form className="composer" onSubmit={handleSubmit}>
        <textarea
          className="composer-input"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              handleSubmit(e);
            }
          }}
          placeholder={status === "connected" ? "Message your coach…" : "Connecting…"}
          rows={1}
          disabled={status !== "connected"}
        />
        <button
          type="submit"
          className="send-button"
          disabled={status !== "connected" || !draft.trim()}
          aria-label="Send message"
        >
          <PlateGlyph size={20} />
        </button>
      </form>
    </div>
  );
}
