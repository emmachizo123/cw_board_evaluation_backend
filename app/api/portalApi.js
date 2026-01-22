/**
 * src/api/portalApi.js
 * --------------------
 * Board Member (Participant) Portal API client.
 *
 * This module wraps the FastAPI token-based portal endpoints:
 *  - GET  /api/v1/portal/{token}
 *  - GET  /api/v1/portal/{token}/responses
 *  - POST /api/v1/portal/{token}/responses?finalize=true|false
 *
 * Configuration:
 *  - Set VITE_API_BASE_URL in your .env (e.g., "http://127.0.0.1:8000")
 *  - If unset, API_BASE defaults to "" (same origin).
 *
 * Error handling:
 *  - If the HTTP response is not OK, these functions throw an Error
 *    whose message is the raw response text from the server.
 */

const API_BASE = import.meta.env.VITE_API_BASE_URL || "";

/**
 * Load portal context for a participant token.
 *
 * Calls: GET /api/v1/portal/{token}
 *
 * @param {string} token - Participant access token (from invite link).
 * @returns {Promise<object>} Portal payload containing participant, evaluation, and active questions.
 * @throws {Error} If the server returns a non-2xx response.
 */
export async function portalLoad(token) {
  const r = await fetch(`${API_BASE}/api/v1/portal/${token}`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

/**
 * Fetch existing responses for a participant token (resume / prefill support).
 *
 * Calls: GET /api/v1/portal/{token}/responses
 *
 * @param {string} token - Participant access token.
 * @returns {Promise<object>} Payload containing participant_id, evaluation_id, count, and items[].
 * @throws {Error} If the server returns a non-2xx response.
 */
export async function portalGetResponses(token) {
  const r = await fetch(`${API_BASE}/api/v1/portal/${token}/responses`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

/**
 * Submit responses for a participant token.
 *
 * Calls: POST /api/v1/portal/{token}/responses?finalize=true|false
 *
 * @param {string} token - Participant access token.
 * @param {Array<{question_id: string, score?: number|null, comment?: string|null}>} answers
 *   List of answers. Each answer must match the question answer_type rules:
 *   - rating: score required (1..5)
 *   - yesno:  score required (0 or 1)
 *   - comment: comment required (non-empty)
 * @param {boolean} [finalize=true] - If true, marks participant status as "responded".
 * @returns {Promise<object>} Submission result (created/updated/skipped/finalized/status).
 * @throws {Error} If the server returns a non-2xx response.
 */
export async function portalSubmitResponses(token, answers, finalize = true) {
  const url = `${API_BASE}/api/v1/portal/${token}/responses?finalize=${finalize ? "true" : "false"}`;

  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ answers }),
  });

  if (!r.ok) throw new Error(await r.text());
  return r.json();
}
