/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_FLEET_REPLAY?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
