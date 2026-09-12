/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Backend API origin, e.g. "http://localhost:8000". Defaults to localhost:8000 if unset. */
  readonly VITE_API_BASE_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
