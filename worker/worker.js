// 텔레그램 웹훅 수신기 (Cloudflare Worker, 무료 플랜)
//
// 봇에 메시지가 오면 텔레그램이 곧바로 여기로 보낸다. 유튜브 링크가 있으면 GitHub Actions(telegram.yml)를
// 바로 실행시키고, 같은 응답으로 "접수했어" 답장을 보낸다. 요약과 요약 답장은 Actions가 한다.
//
// 비밀값 (npx wrangler secret put <이름>):
//   WEBHOOK_SECRET   텔레그램 setWebhook에 준 secret_token. 이 값이 없는 요청은 거절한다 (아무나 Actions를 못 돌리게)
//   TELEGRAM_CHAT_ID 처리할 채팅 (내 채팅만)
//   GITHUB_TOKEN     tube-digest 저장소에 "Actions: Read and write" 권한만 준 fine-grained 토큰

const YOUTUBE = /(?:youtube\.com\/(?:watch\?(?:[^\s#]*&)?v=|shorts\/|live\/|embed\/)|youtu\.be\/)[\w-]{11}/;
const HELP = "유튜브 영상 링크를 보내면 요약해서 답장할게. 보통 1~2분 걸려.";

// 웹훅 응답 본문에 Bot API 호출을 담으면 텔레그램이 대신 실행한다. 그래서 봇 토큰 없이도 바로 답장할 수 있다.
function reply(chatId, messageId, text) {
  return Response.json({
    method: "sendMessage",
    chat_id: chatId,
    text,
    reply_parameters: { message_id: messageId, allow_sending_without_reply: true },
  });
}

export default {
  async fetch(request, env) {
    if (request.method !== "POST") return new Response("tube-digest webhook");
    if (!env.WEBHOOK_SECRET || request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.WEBHOOK_SECRET) {
      return new Response("forbidden", { status: 403 });
    }
    const update = await request.json();
    const msg = update.message;
    // 내 채팅이 아니면 무시한다. 200을 돌려줘야 텔레그램이 같은 메시지를 다시 보내지 않는다.
    if (!msg || String(msg.chat?.id) !== String(env.TELEGRAM_CHAT_ID)) return new Response("ignored");

    const text = msg.text || msg.caption || "";
    if (!YOUTUBE.test(text)) return text.trim() ? reply(msg.chat.id, msg.message_id, HELP) : new Response("ok");

    const payload = { update_id: update.update_id, message: { message_id: msg.message_id, chat: { id: msg.chat.id }, text } };
    const r = await fetch(
      `https://api.github.com/repos/${env.GITHUB_REPO}/actions/workflows/telegram.yml/dispatches`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${env.GITHUB_TOKEN}`,
          Accept: "application/vnd.github+json",
          "X-GitHub-Api-Version": "2022-11-28",
          "User-Agent": "tube-digest-webhook",
        },
        body: JSON.stringify({ ref: "main", inputs: { update: JSON.stringify(payload) } }),
      },
    );
    if (!r.ok) {
      // 실패를 돌려주면 텔레그램이 잠시 뒤 같은 메시지를 다시 보낸다 (토큰 만료 등은 wrangler tail 로그로 확인)
      console.log("GitHub dispatch failed", r.status, await r.text());
      return new Response("dispatch failed", { status: 502 });
    }
    return reply(msg.chat.id, msg.message_id, "접수했어. 1~2분 안에 요약해서 답장할게.");
  },
};
