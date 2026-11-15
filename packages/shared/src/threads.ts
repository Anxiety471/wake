import type { WakeReport } from './wake.js';

export type ThreadKind = 'ask' | 'thread';

export type ThreadStatus = 'open' | 'answered' | 'closed';

export type ThreadParticipant = {
  name: string;
};

export type ThreadMessage = {
  id: string;
  seq: number;
  from: string;
  body: string;
  createdAt: string;
};

export type ThreadDetail = {
  id: string;
  kind: ThreadKind;
  status: ThreadStatus;
  createdBy: string;
  createdAt: string;
  updatedAt: string;
  expiresAt: string | null;
  expired: boolean;
  lastSeq: number;
  myReadSeq: number;
  participants: ThreadParticipant[];
  messages: ThreadMessage[];
};

export type ThreadSummary = {
  id: string;
  kind: ThreadKind;
  status: ThreadStatus;
  createdBy: string;
  createdAt: string;
  updatedAt: string;
  expired: boolean;
  lastSeq: number;
  myReadSeq: number;
  unread: number;
  participants: ThreadParticipant[];
};

export type ThreadPage = {
  threads: ThreadSummary[];
  next: string | null;
};

export type CreatedThread = {
  threadId: string;
  messageId: string;
  seq: number;
  createdAt: string;
  wake: WakeReport;
};

export type ThreadReply = CreatedThread & {
  status: ThreadStatus;
};

export type ThreadReadReceipt = {
  threadId: string;
  readSeq: number;
};
