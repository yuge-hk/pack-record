import { inject } from 'vue'
import type { InjectionKey } from 'vue'
import type { ApiClient } from './api'
export interface ConsoleContext { api: ApiClient; run: (job: () => Promise<void>) => Promise<void>; notify: (message: string) => void }
export const contextKey: InjectionKey<ConsoleContext> = Symbol('console')
export function useConsole() { const value = inject(contextKey); if (!value) throw new Error('Console unavailable'); return value }
