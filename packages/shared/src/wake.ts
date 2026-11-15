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
// `deliveries` with `enabled` true means no recipient had a target.
export type WakeReport = {
  enabled: boolean;
  deliveries: WakeDelivery[];
};
