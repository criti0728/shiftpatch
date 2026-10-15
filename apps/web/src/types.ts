export type Screen = "service" | "shifts" | "prep" | "close";
export type ProposalMode = "pre_open" | "in_service" | "post_close";
export type Objective = "balanced" | "preserve_drinks" | "reduce_delay";

export interface FirebaseConfig {
  apiKey: string;
  authDomain: string;
  projectId: string;
  appId: string;
}
export interface SessionSettings {
  firebase_enabled: boolean;
  firebase_config?: FirebaseConfig | null;
  has_saved_guest?: boolean;
}
export interface AuthenticatedSession extends SessionSettings {
  authenticated: true;
  user: { id: string; kind: "guest" | "firebase"; display_name: string };
  store: { id: string; name: string };
}
export type Session = SessionSettings &
  ({ authenticated: false } | AuthenticatedSession);

export interface Staff {
  id: string;
  name: string;
  role: string;
  absent: boolean;
  shift_start: string;
  shift_end: string;
  skills: Record<string, number>;
  available_dates?: string[];
}

export interface Menu {
  id: string;
  name: string;
  category: "drink" | "food";
  price: number;
  station: string;
  recipe: Record<string, number>;
  emoji: string;
  available_units: number;
  limit_remaining: number | null;
}

export interface Ingredient {
  id: string;
  name: string;
  unit: string;
  on_hand: number;
  reserved: number;
  expires_at: string;
  buffer: number;
  lots?: { id: string; quantity: number; reserved: number; expires_at: string; received_at: string }[];
}

export interface Delivery {
  id: string;
  ingredient_id: string;
  quantity: number;
  arrives_at: string;
  confirmed: boolean;
  received: boolean;
}

export interface Task {
  id: string;
  menu_id: string;
  status: "accepted" | "preparing" | "completed";
  staff_id: string | null;
  station: string;
  started_at: string | null;
  duration_seconds: number | null;
  allocations: Record<string, number>;
  lot_allocations?: Record<string, { lot_id: string; quantity: number }[]>;
}

export interface Order {
  id: string;
  number: number | string;
  created_at: string;
  status: "accepted" | "preparing" | "completed";
  items: {
    menu_id: string;
    name: string;
    quantity: number;
    unit_price: number;
  }[];
  total: number;
  tasks: Task[];
}

export interface Candidate {
  id: string;
  title: string;
  action: "keep_current" | "replace_limits";
  limits: Record<string, number>;
  review_after_minutes: number;
  valid_until?: string;
  metrics: {
    completed_units: number;
    completed_orders: number;
    rejected_units: number;
    pending_units: number;
    max_wait_minutes: number;
    revenue: number;
    existing_at_risk: number;
  };
  feasible: boolean;
  notes: string[];
  explanation?: string;
  tradeoff?: string;
  parent_candidate_id?: string;
  based_on_result_ids?: string[];
  evidence_refs?: string[];
}

export interface Prep {
  service_date: string;
  forecast: Record<string, number>;
  planned?: Record<string, number>;
  items: {
    ingredient_id: string;
    name: string;
    unit: string;
    needed: number;
    available: number;
    incoming: number;
    buffer: number;
    to_order: number;
  }[];
  notes: string[];
  windows?: { start_at: string; end_at: string; counts: Record<string, number> }[];
}

export interface Proposal {
  id: string;
  mode: ProposalMode;
  type: "prep_plan" | "service_plan" | "next_day_plan";
  status:
    "ready" | "stale" | "applied" | "acknowledged" | "failed" | "waiting_input" | "dismissed";
  revision: number;
  source: "gemini" | "calculation_only";
  objective?: Objective;
  created_at: string;
  review_at: string | null;
  recommended_candidate_id: string | null;
  summary: string;
  candidates: Candidate[];
  trace: {
    step: number;
    origin: "server" | "model";
    tool: string;
    args: unknown;
    result_summary: unknown;
  }[];
  prep?: Prep;
  run_id?: string;
  parent_run_id?: string | null;
  model_id?: string;
  model_calls?: number;
  elapsed_ms?: number;
  questions?: (AgentQuestion | string)[];
  error?: string;
  provider_http_status?: number | null;
  snapshot?: Partial<Pick<Overview, "store" | "business_now" | "revision" | "staff" | "next_day_staff" | "inventory" | "history" | "closes">>;
}

export interface AgentQuestion {
  id: string;
  text: string;
  field: "staff" | "next_day_staff" | "inventory" | "history" | "close" | "objective" | "allow_menu_pause" | "review_after_minutes";
  options: string[];
}
export interface GenerateOptions {
  objective?: Objective;
  trigger_event_id?: string;
  parent_run_id?: string;
  answers?: Record<string, string>;
}
export interface AgentRun {
  id: string;
  status: string;
  phase: "snapshot" | "investigate" | "calculate" | "explain" | "validate";
  trace: Proposal["trace"];
  cancel_requested?: boolean;
}
export interface CloseRecord {
  id: string;
  service_date?: string;
  closed_at?: string;
  at?: string;
  revenue?: number;
  completed_orders?: number;
  waiting_orders?: number;
  notes?: string[];
  forecast_source?: "acknowledged_prep_plan" | "historical_baseline_at_close";
  forecast_proposal_id?: string | null;
  counts?: Record<string, number>;
  forecast?: Record<string, number>;
  variance?: Record<string, number>;
  inventory?: Ingredient[];
  restricted?: boolean;
  [key: string]: unknown;
}

export interface Overview {
  store: { id: string; name: string; service_date: string; open_time?: string; close_time?: string; timezone?: string };
  business_now: string;
  clock?: { mode: "manual" | "realtime" };
  revision: number;
  staff: Staff[];
  next_day_staff?: Staff[];
  menus: Menu[];
  inventory: Ingredient[];
  deliveries: Delivery[];
  orders: Order[];
  mode: {
    id: string;
    limits: Record<string, number> | null;
    used: Record<string, number>;
    starts_at: string;
    review_at: string | null;
    status: string;
  };
  proposals: Proposal[];
  closes: CloseRecord[];
  events: { id: string; type: string; at: string; message: string }[];
  history: {
    date: string;
    counts: Record<string, number>;
    restricted: boolean;
    windows?: { start_time: string; counts: Record<string, number> }[];
  }[];
  replan_required?: boolean;
  replan_event?: { id: string; message: string } | null;
  active_run?: AgentRun | null;
  metrics: {
    completed_orders: number;
    revenue: number;
    waiting_orders: number;
  };
  config: {
    agent_mode: "gemini" | "calculation_only";
    storage: "sqlite" | "firestore";
    auth: "demo" | "guest" | "firebase";
  };
}
