export type WakeDeliveryReason =
  | 'no_routine'
  | 'unauthorized'
  | 'payload_too_large'
  | 'rate_limited'
  | 'rejected'
  | 'bad_response'
  | 'server_error'
  | 'network'
  | 'timeout';

export type WakeDelivery = {
  agent: string;
  delivered: boolean;
  reason: WakeDeliveryReason | null;
  attempts: number;
};

// What happened to the doorbell for one sent message: `enabled` false means the
// server has no doorbell targets configured and nothing was rung. An empty
// `deliveries` with `enabled` true means no recipient had a target; `error`
// says the recipients could not be read at all, so nobody could be rung.
export type WakeReport = {
  enabled: boolean;
  deliveries: WakeDelivery[];
  error?: 'recipients_unreadable';
};

// One persisted delivery outcome. `status` is the last HTTP status the doorbell
// saw, or null when no response was read (network error, timeout, local cap).
export type DeliveryOutcome = {
  agent: string;
  delivered: boolean;
  reason: WakeDeliveryReason | null;
  attempts: number;
  status: number | null;
  at: string;
};

export type MessageDeliveries = {
  messageId: string;
  deliveries: DeliveryOutcome[];
};
