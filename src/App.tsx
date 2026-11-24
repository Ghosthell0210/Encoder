import { useMemo, useState } from 'react';
import {
  Check,
  Copy,
  Download,
  FileCode2,
  FolderOpen,
  Search,
} from 'lucide-react';
import {
  BOT_FILES,
  DEFAULT_ENV_FORM,
  type BotFileItem,
  type EnvFormState,
  buildEnvFileContent,
  downloadBotZip,
  downloadSingleTextFile,
} from './data/botFiles';

type ActiveTab = 'files' | 'env' | 'deploy';

const GROUPS: BotFileItem['group'][] = ['Raíz', 'core/', 'handlers/'];

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  return `${(bytes / 1024).toFixed(1)} KiB`;
}

export default function App() {
  const [activeTab, setActiveTab] = useState<ActiveTab>('files');
  const [selectedPath, setSelectedPath] = useState<string>('bot.py');
  const [searchQuery, setSearchQuery] = useState<string>('');
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const [isZipping, setIsZipping] = useState<boolean>(false);
  const [envForm, setEnvForm] = useState<EnvFormState>(DEFAULT_ENV_FORM);

  const filteredFiles = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    if (!q) return BOT_FILES;
    return BOT_FILES.filter(
      (f) =>
        f.path.toLowerCase().includes(q) ||
        f.summary.toLowerCase().includes(q),
    );
  }, [searchQuery]);

  const selectedFile = useMemo(
    () => BOT_FILES.find((f) => f.path === selectedPath) ?? BOT_FILES[0],
    [selectedPath],
  );

  const envPreview = useMemo(() => buildEnvFileContent(envForm), [envForm]);
  const totalLines = useMemo(
    () => BOT_FILES.reduce((acc, f) => acc + f.lines, 0),
    [],
  );

  async function handleCopy(id: string, text: string) {
    try {
      await navigator.clipboard.writeText(text);
      setCopiedId(id);
      window.setTimeout(() => {
        setCopiedId((cur) => (cur === id ? null : cur));
      }, 1600);
    } catch {
      // Fallback silencioso si el iframe bloquea clipboard
    }
  }

  async function handleDownloadZip() {
    setIsZipping(true);
    try {
      await downloadBotZip(envForm);
    } finally {
      setIsZipping(false);
    }
  }

  function updateEnv<K extends keyof EnvFormState>(key: K, value: EnvFormState[K]) {
    setEnvForm((prev) => ({ ...prev, [key]: value }));
  }

  return (
    <div className="min-h-screen flex flex-col bg-[#0B111E] text-[#F3F4F6]">
      {/* Barra superior: contrato estricto de 3 zonas */}
      <header className="sticky top-0 z-20 h-16 px-6 flex items-center justify-between gap-8 bg-[#0B111E]/95 backdrop-blur border-b border-[#1F2937]">
        <span className="text-base font-bold tracking-tight text-[#F3F4F6] whitespace-nowrap shrink-0">
          HydroCompress Bot
        </span>

        <nav className="flex items-center gap-6 text-sm font-medium">
          <button
            type="button"
            onClick={() => setActiveTab('files')}
            className={`py-1 transition-colors whitespace-nowrap shrink-0 cursor-pointer border-b-2 ${
              activeTab === 'files'
                ? 'text-[#F3F4F6] border-[#2563EB]'
                : 'text-[#9CA3AF] border-transparent hover:text-[#F3F4F6]'
            }`}
          >
            Archivos del Bot
          </button>
          <button
            type="button"
            onClick={() => setActiveTab('env')}
            className={`py-1 transition-colors whitespace-nowrap shrink-0 cursor-pointer border-b-2 ${
              activeTab === 'env'
                ? 'text-[#F3F4F6] border-[#2563EB]'
                : 'text-[#9CA3AF] border-transparent hover:text-[#F3F4F6]'
            }`}
          >
            Configurar .env
          </button>
          <button
            type="button"
            onClick={() => setActiveTab('deploy')}
            className={`py-1 transition-colors whitespace-nowrap shrink-0 cursor-pointer border-b-2 ${
              activeTab === 'deploy'
                ? 'text-[#F3F4F6] border-[#2563EB]'
                : 'text-[#9CA3AF] border-transparent hover:text-[#F3F4F6]'
            }`}
          >
            Guía plugged.host
          </button>
        </nav>

        <button
          type="button"
          onClick={handleDownloadZip}
          disabled={isZipping}
          className="inline-flex items-center gap-2 px-4 py-2 text-sm font-semibold text-white bg-[#2563EB] hover:bg-[#1D4ED8] disabled:opacity-60 rounded-lg transition-colors whitespace-nowrap shrink-0 cursor-pointer"
        >
          <Download className="w-4 h-4" />
          <span>{isZipping ? 'Empaquetando...' : 'Descargar ZIP'}</span>
        </button>
      </header>

      {/* Franja limpia de metadatos sin píldoras ni cajas decorativas */}
      <div className="px-6 py-3 border-b border-[#1F2937] bg-[#111827] flex flex-wrap items-center justify-between gap-4 text-xs text-[#9CA3AF]">
        <div className="flex flex-wrap items-center gap-2 font-mono tabular-nums">
          <span className="text-[#F3F4F6] font-medium">Python 3.11 + Hydrogram + FFmpeg</span>
          <span aria-hidden="true">·</span>
          <span>400% CPU (4 hilos)</span>
          <span aria-hidden="true">·</span>
          <span>10 GiB RAM</span>
          <span aria-hidden="true">·</span>
          <span>30 GiB Disco</span>
          <span aria-hidden="true">·</span>
          <span>Telegram ≤ 2 GB / Enlace automático &gt; 2 GB</span>
        </div>
        <div className="flex items-center gap-2 font-mono tabular-nums">
          <span>Bot sin servidor web</span>
          <span aria-hidden="true">·</span>
          <span>{BOT_FILES.length} archivos</span>
          <span aria-hidden="true">·</span>
          <span>{totalLines.toLocaleString('es-ES')} líneas</span>
        </div>
      </div>

      {/* Contenido principal */}
      {activeTab === 'files' && (
        <main className="flex-1 flex flex-col lg:flex-row min-h-0">
          {/* Barra lateral de archivos */}
          <aside className="w-full lg:w-72 shrink-0 border-b lg:border-b-0 lg:border-r border-[#1F2937] bg-[#111827] flex flex-col">
            <div className="p-4 border-b border-[#1F2937]">
              <label className="relative block">
                <Search className="w-4 h-4 text-[#9CA3AF] absolute left-3 top-1/2 -translate-y-1/2 pointer-events-none" />
                <input
                  type="text"
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  placeholder="Filtrar archivos..."
                  className="w-full pl-9 pr-3 py-2 text-sm bg-[#0B111E] border border-[#1F2937] rounded-md text-[#F3F4F6] placeholder-[#6B7280] focus:outline-none focus:border-[#2563EB]"
                />
              </label>
            </div>

            <div className="flex-1 overflow-y-auto p-3 space-y-5">
              {GROUPS.map((group) => {
                const items = filteredFiles.filter((f) => f.group === group);
                if (items.length === 0) return null;
                return (
                  <div key={group}>
                    <div className="px-2 pb-1.5 flex items-center gap-2 text-xs font-medium text-[#9CA3AF]">
                      <FolderOpen className="w-3.5 h-3.5" />
                      <span>{group}</span>
                    </div>
                    <div className="space-y-0.5">
                      {items.map((file) => {
                        const isActive = file.path === selectedFile.path;
                        return (
                          <button
                            key={file.path}
                            type="button"
                            onClick={() => setSelectedPath(file.path)}
                            className={`w-full px-2.5 py-2 rounded-md text-left flex items-center justify-between gap-2 transition-colors cursor-pointer ${
                              isActive
                                ? 'bg-[#1F2937] text-[#F3F4F6]'
                                : 'text-[#9CA3AF] hover:bg-[#1A2233] hover:text-[#F3F4F6]'
                            }`}
                          >
                            <span className="flex items-center gap-2 min-w-0">
                              <FileCode2 className="w-3.5 h-3.5 shrink-0 text-[#60A5FA]" />
                              <span className="text-xs font-mono truncate">{file.name}</span>
                            </span>
                            <span className="text-[11px] font-mono tabular-nums text-[#6B7280] shrink-0">
                              {file.lines}L
                            </span>
                          </button>
                        );
                      })}
                    </div>
                  </div>
                );
              })}

              {filteredFiles.length === 0 && (
                <p className="px-2 py-4 text-xs text-[#9CA3AF]">
                  Ningún archivo coincide con "{searchQuery}".
                </p>
              )}
            </div>
          </aside>

          {/* Visor de código */}
          <section className="flex-1 min-w-0 flex flex-col bg-[#0B111E]">
            <div className="px-6 py-4 border-b border-[#1F2937] flex flex-wrap items-center justify-between gap-4">
              <div className="min-w-0">
                <h1 className="text-sm font-mono font-semibold text-[#F3F4F6] truncate">
                  {selectedFile.path}
                </h1>
                <p className="mt-1 text-xs text-[#9CA3AF]">{selectedFile.summary}</p>
              </div>

              <div className="flex items-center gap-3 shrink-0">
                <span className="text-xs font-mono tabular-nums text-[#9CA3AF]">
                  {selectedFile.lines} líneas · {formatBytes(selectedFile.bytes)}
                </span>
                <button
                  type="button"
                  onClick={() => handleCopy(selectedFile.path, selectedFile.content)}
                  className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-[#F3F4F6] bg-[#111827] hover:bg-[#1F2937] border border-[#1F2937] rounded-md transition-colors whitespace-nowrap shrink-0 cursor-pointer"
                >
                  {copiedId === selectedFile.path ? (
                    <>
                      <Check className="w-3.5 h-3.5 text-[#16A34A]" />
                      <span>Copiado</span>
                    </>
                  ) : (
                    <>
                      <Copy className="w-3.5 h-3.5" />
                      <span>Copiar</span>
                    </>
                  )}
                </button>
                <button
                  type="button"
                  onClick={() =>
                    downloadSingleTextFile(selectedFile.name, selectedFile.content)
                  }
                  className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-[#F3F4F6] bg-[#111827] hover:bg-[#1F2937] border border-[#1F2937] rounded-md transition-colors whitespace-nowrap shrink-0 cursor-pointer"
                >
                  <Download className="w-3.5 h-3.5" />
                  <span>Guardar</span>
                </button>
              </div>
            </div>

            <div className="flex-1 overflow-auto p-6">
              <pre className="text-[13px] leading-6 font-mono text-[#E5E7EB] tab-size-4">
                {selectedFile.content.split('\n').map((line, idx) => (
                  <div key={idx} className="flex hover:bg-[#111827]/70">
                    <span className="w-12 pr-4 select-none text-right text-[#4B5563] tabular-nums shrink-0">
                      {idx + 1}
                    </span>
                    <span className="whitespace-pre">{line || ' '}</span>
                  </div>
                ))}
              </pre>
            </div>
          </section>
        </main>
      )}

      {activeTab === 'env' && (
        <main className="flex-1 max-w-6xl w-full mx-auto px-6 py-8 grid grid-cols-1 lg:grid-cols-12 gap-8 items-start">
          {/* Formulario local */}
          <section className="lg:col-span-7 bg-[#111827] border border-[#1F2937] rounded-lg p-6 space-y-6">
            <div>
              <h1 className="text-lg font-semibold text-[#F3F4F6]">
                Generador local de archivo .env (opcional)
              </h1>
              <p className="mt-1 text-sm text-[#9CA3AF]">
                Todo permanece en tu navegador. Si activas la casilla inferior, el botón Descargar ZIP incluirá el archivo .env ya listo para subir a panel.plugged.host.
              </p>
            </div>

            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              <label className="block space-y-1.5">
                <span className="text-xs font-medium text-[#D1D5DB]">API_ID (my.telegram.org)</span>
                <input
                  type="text"
                  inputMode="numeric"
                  value={envForm.apiId}
                  onChange={(e) => updateEnv('apiId', e.target.value)}
                  placeholder="12345678"
                  className="w-full px-3 py-2 text-sm font-mono bg-[#0B111E] border border-[#1F2937] rounded-md text-[#F3F4F6] placeholder-[#4B5563] focus:outline-none focus:border-[#2563EB]"
                />
              </label>

              <label className="block space-y-1.5">
                <span className="text-xs font-medium text-[#D1D5DB]">OWNER_ID (tu ID numérico)</span>
                <input
                  type="text"
                  inputMode="numeric"
                  value={envForm.ownerId}
                  onChange={(e) => updateEnv('ownerId', e.target.value)}
                  placeholder="123456789"
                  className="w-full px-3 py-2 text-sm font-mono bg-[#0B111E] border border-[#1F2937] rounded-md text-[#F3F4F6] placeholder-[#4B5563] focus:outline-none focus:border-[#2563EB]"
                />
              </label>

              <label className="block space-y-1.5 sm:col-span-2">
                <span className="text-xs font-medium text-[#D1D5DB]">API_HASH (my.telegram.org)</span>
                <input
                  type="text"
                  value={envForm.apiHash}
                  onChange={(e) => updateEnv('apiHash', e.target.value)}
                  placeholder="a1b2c3d4e5f6..."
                  className="w-full px-3 py-2 text-sm font-mono bg-[#0B111E] border border-[#1F2937] rounded-md text-[#F3F4F6] placeholder-[#4B5563] focus:outline-none focus:border-[#2563EB]"
                />
              </label>

              <label className="block space-y-1.5 sm:col-span-2">
                <span className="text-xs font-medium text-[#D1D5DB]">BOT_TOKEN (@BotFather)</span>
                <input
                  type="password"
                  value={envForm.botToken}
                  onChange={(e) => updateEnv('botToken', e.target.value)}
                  placeholder="123456789:AAH..."
                  className="w-full px-3 py-2 text-sm font-mono bg-[#0B111E] border border-[#1F2937] rounded-md text-[#F3F4F6] placeholder-[#4B5563] focus:outline-none focus:border-[#2563EB]"
                />
              </label>

              <label className="block space-y-1.5 sm:col-span-2">
                <span className="text-xs font-medium text-[#D1D5DB]">
                  USER_SESSION_STRING (opcional — también puedes usar /login en Telegram)
                </span>
                <input
                  type="password"
                  value={envForm.userSessionString}
                  onChange={(e) => updateEnv('userSessionString', e.target.value)}
                  placeholder="Vacío si iniciarás sesión después con /login"
                  className="w-full px-3 py-2 text-sm font-mono bg-[#0B111E] border border-[#1F2937] rounded-md text-[#F3F4F6] placeholder-[#4B5563] focus:outline-none focus:border-[#2563EB]"
                />
              </label>

              <label className="block space-y-1.5 sm:col-span-2">
                <span className="text-xs font-medium text-[#D1D5DB]">
                  DUMP_CHAT_ID (opcional — canal privado puente para videos 2–4 GB con Premium)
                </span>
                <input
                  type="text"
                  value={envForm.dumpChatId}
                  onChange={(e) => updateEnv('dumpChatId', e.target.value)}
                  placeholder="-1001234567890"
                  className="w-full px-3 py-2 text-sm font-mono bg-[#0B111E] border border-[#1F2937] rounded-md text-[#F3F4F6] placeholder-[#4B5563] focus:outline-none focus:border-[#2563EB]"
                />
              </label>
            </div>

            <div className="pt-4 border-t border-[#1F2937] space-y-4">
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                <div className="space-y-1.5">
                  <span className="block text-xs font-medium text-[#D1D5DB]">Control de acceso</span>
                  <div className="grid grid-cols-2 gap-1 p-1 bg-[#0B111E] border border-[#1F2937] rounded-md">
                    <button
                      type="button"
                      onClick={() => updateEnv('accessMode', 'private')}
                      className={`py-1.5 px-2 text-xs font-medium rounded transition-colors cursor-pointer whitespace-nowrap ${
                        envForm.accessMode === 'private'
                          ? 'bg-[#2563EB] text-white'
                          : 'text-[#9CA3AF] hover:text-[#F3F4F6]'
                      }`}
                    >
                      Lista Blanca
                    </button>
                    <button
                      type="button"
                      onClick={() => updateEnv('accessMode', 'public')}
                      className={`py-1.5 px-2 text-xs font-medium rounded transition-colors cursor-pointer whitespace-nowrap ${
                        envForm.accessMode === 'public'
                          ? 'bg-[#2563EB] text-white'
                          : 'text-[#9CA3AF] hover:text-[#F3F4F6]'
                      }`}
                    >
                      Público
                    </button>
                  </div>
                </div>

                <div className="space-y-1.5">
                  <span className="block text-xs font-medium text-[#D1D5DB]">Concurrencia (400% CPU)</span>
                  <div className="grid grid-cols-2 gap-1 p-1 bg-[#0B111E] border border-[#1F2937] rounded-md">
                    <button
                      type="button"
                      onClick={() => updateEnv('maxConcurrentJobs', '1')}
                      className={`py-1.5 px-2 text-xs font-medium rounded transition-colors cursor-pointer whitespace-nowrap ${
                        envForm.maxConcurrentJobs === '1'
                          ? 'bg-[#2563EB] text-white'
                          : 'text-[#9CA3AF] hover:text-[#F3F4F6]'
                      }`}
                    >
                      1 × 4 hilos
                    </button>
                    <button
                      type="button"
                      onClick={() => updateEnv('maxConcurrentJobs', '2')}
                      className={`py-1.5 px-2 text-xs font-medium rounded transition-colors cursor-pointer whitespace-nowrap ${
                        envForm.maxConcurrentJobs === '2'
                          ? 'bg-[#2563EB] text-white'
                          : 'text-[#9CA3AF] hover:text-[#F3F4F6]'
                      }`}
                    >
                      2 × 2 hilos
                    </button>
                  </div>
                </div>
              </div>

              <div className="space-y-1.5">
                <span className="block text-xs font-medium text-[#D1D5DB]">
                  Proveedor de enlace automático para videos &gt; 2 GB
                </span>
                <div className="grid grid-cols-3 gap-1 p-1 bg-[#0B111E] border border-[#1F2937] rounded-md">
                  {(
                    [
                      ['gofile', 'GoFile (Auto)'],
                      ['buzzheavier', 'Buzzheavier'],
                      ['direct_http', 'Puerto propio'],
                    ] as const
                  ).map(([id, label]) => (
                    <button
                      key={id}
                      type="button"
                      onClick={() => updateEnv('largeFileUploader', id)}
                      className={`py-1.5 px-2 text-xs font-medium rounded transition-colors cursor-pointer whitespace-nowrap ${
                        envForm.largeFileUploader === id
                          ? 'bg-[#2563EB] text-white'
                          : 'text-[#9CA3AF] hover:text-[#F3F4F6]'
                      }`}
                    >
                      {label}
                    </button>
                  ))}
                </div>
              </div>

              <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                <label className="block space-y-1.5">
                  <span className="text-xs font-medium text-[#D1D5DB]">GOFILE_TOKEN (opcional)</span>
                  <input
                    type="text"
                    value={envForm.gofileToken}
                    onChange={(e) => updateEnv('gofileToken', e.target.value)}
                    placeholder="Vacío para subida anónima"
                    className="w-full px-3 py-2 text-sm font-mono bg-[#0B111E] border border-[#1F2937] rounded-md text-[#F3F4F6] placeholder-[#4B5563] focus:outline-none focus:border-[#2563EB]"
                  />
                </label>

                <label className="block space-y-1.5">
                  <span className="text-xs font-medium text-[#D1D5DB]">
                    PUBLIC_DOWNLOAD_BASE_URL (opcional)
                  </span>
                  <input
                    type="text"
                    value={envForm.publicDownloadBaseUrl}
                    onChange={(e) => updateEnv('publicDownloadBaseUrl', e.target.value)}
                    placeholder="http://tu-ip:puerto"
                    className="w-full px-3 py-2 text-sm font-mono bg-[#0B111E] border border-[#1F2937] rounded-md text-[#F3F4F6] placeholder-[#4B5563] focus:outline-none focus:border-[#2563EB]"
                  />
                </label>
              </div>
            </div>

            <label className="pt-4 border-t border-[#1F2937] flex items-center gap-3 cursor-pointer">
              <input
                type="checkbox"
                checked={envForm.includeEnvInZip}
                onChange={(e) => updateEnv('includeEnvInZip', e.target.checked)}
                className="w-4 h-4 rounded accent-[#2563EB]"
              />
              <span className="text-sm text-[#E5E7EB]">
                Incluir este archivo <code className="font-mono text-[#60A5FA]">.env</code> al pulsar Descargar ZIP
              </span>
            </label>
          </section>

          {/* Vista previa del .env */}
          <section className="lg:col-span-5 bg-[#111827] border border-[#1F2937] rounded-lg overflow-hidden">
            <div className="px-4 py-3 border-b border-[#1F2937] flex items-center justify-between gap-3">
              <span className="text-xs font-mono font-semibold text-[#F3F4F6]">.env</span>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => handleCopy('env-preview', envPreview)}
                  className="inline-flex items-center gap-1.5 px-2.5 py-1 text-xs font-medium text-[#F3F4F6] bg-[#0B111E] hover:bg-[#1F2937] border border-[#1F2937] rounded transition-colors cursor-pointer"
                >
                  {copiedId === 'env-preview' ? (
                    <>
                      <Check className="w-3.5 h-3.5 text-[#16A34A]" />
                      <span>Copiado</span>
                    </>
                  ) : (
                    <>
                      <Copy className="w-3.5 h-3.5" />
                      <span>Copiar</span>
                    </>
                  )}
                </button>
                <button
                  type="button"
                  onClick={() => downloadSingleTextFile('.env', envPreview)}
                  className="inline-flex items-center gap-1.5 px-2.5 py-1 text-xs font-medium text-[#F3F4F6] bg-[#0B111E] hover:bg-[#1F2937] border border-[#1F2937] rounded transition-colors cursor-pointer"
                >
                  <Download className="w-3.5 h-3.5" />
                  <span>Guardar .env</span>
                </button>
              </div>
            </div>
            <pre className="p-4 text-xs leading-6 font-mono text-[#D1D5DB] overflow-x-auto">
              {envPreview}
            </pre>
          </section>
        </main>
      )}

      {activeTab === 'deploy' && (
        <main className="flex-1 max-w-5xl w-full mx-auto px-6 py-8 space-y-8">
          <section className="space-y-2">
            <h1 className="text-2xl font-bold tracking-tight text-[#F3F4F6] text-balance">
              Despliegue directo en panel.plugged.host
            </h1>
            <p className="text-sm text-[#9CA3AF] max-w-2xl leading-relaxed">
              Calibrado a los recursos libres de tu captura (400% CPU, 13 GiB RAM y 35 GiB Disco). El bot corre 100% en Telegram sin abrir interfaces web ni consumir recursos innecesarios.
            </p>
          </section>

          {/* Tabla de asignación de recursos */}
          <section className="bg-[#111827] border border-[#1F2937] rounded-lg overflow-hidden">
            <div className="px-5 py-3 border-b border-[#1F2937]">
              <h2 className="text-sm font-semibold text-[#F3F4F6]">
                Asignación recomendada en tu panel
              </h2>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-sm text-left">
                <thead className="border-b border-[#1F2937] text-xs text-[#9CA3AF]">
                  <tr>
                    <th className="px-5 py-3 font-medium">Recurso</th>
                    <th className="px-5 py-3 font-medium text-right">Libre en tu servidor</th>
                    <th className="px-5 py-3 font-medium text-right">Asignar a este bot</th>
                    <th className="px-5 py-3 font-medium">Comportamiento configurado</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-[#1F2937] font-mono text-xs tabular-nums">
                  <tr className="hover:bg-[#161F31]">
                    <td className="px-5 py-3 font-sans text-sm text-[#F3F4F6]">CPU</td>
                    <td className="px-5 py-3 text-right text-[#9CA3AF]">400% (4 vCPU)</td>
                    <td className="px-5 py-3 text-right text-[#60A5FA] font-semibold">400%</td>
                    <td className="px-5 py-3 font-sans text-[#D1D5DB]">
                      1 trabajo × 4 hilos (conmutable a 2 × 2 hilos desde /admin)
                    </td>
                  </tr>
                  <tr className="hover:bg-[#161F31]">
                    <td className="px-5 py-3 font-sans text-sm text-[#F3F4F6]">RAM</td>
                    <td className="px-5 py-3 text-right text-[#9CA3AF]">13 GiB</td>
                    <td className="px-5 py-3 text-right text-[#60A5FA] font-semibold">10 GiB</td>
                    <td className="px-5 py-3 font-sans text-[#D1D5DB]">
                      Subida streaming por bloques de 1 MiB (reserva 3 GiB libres)
                    </td>
                  </tr>
                  <tr className="hover:bg-[#161F31]">
                    <td className="px-5 py-3 font-sans text-sm text-[#F3F4F6]">Disco</td>
                    <td className="px-5 py-3 text-right text-[#9CA3AF]">35 GiB</td>
                    <td className="px-5 py-3 text-right text-[#60A5FA] font-semibold">30 GiB</td>
                    <td className="px-5 py-3 font-sans text-[#D1D5DB]">
                      Guardia de espacio 2.2× + reserva 2 GiB + borrado inmediato
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </section>

          {/* Pasos de instalación */}
          <section className="bg-[#111827] border border-[#1F2937] rounded-lg p-6 space-y-5">
            <h2 className="text-sm font-semibold text-[#F3F4F6]">
              1. Obtener API_ID, API_HASH y BOT_TOKEN (para desbloquear 2 GB)
            </h2>
            <ol className="space-y-3 text-sm text-[#D1D5DB] list-decimal list-inside">
              <li>
                Entra a <code className="font-mono text-[#60A5FA]">https://my.telegram.org</code>, inicia sesión con tu número de teléfono y abre <span className="font-medium text-[#F3F4F6]">API development tools</span>.
              </li>
              <li>
                Crea una aplicación (cualquier nombre, plataforma <span className="font-medium text-[#F3F4F6]">Desktop</span>) y copia <code className="font-mono text-[#60A5FA]">App api_id</code> y <code className="font-mono text-[#60A5FA]">App api_hash</code>.
              </li>
              <li>
                Crea tu bot en <code className="font-mono text-[#60A5FA]">@BotFather</code> (<code className="font-mono">/newbot</code>) para el <code className="font-mono">BOT_TOKEN</code> y consulta tu <code className="font-mono">OWNER_ID</code> en <code className="font-mono text-[#60A5FA]">@userinfobot</code>.
              </li>
            </ol>
          </section>

          <section className="bg-[#111827] border border-[#1F2937] rounded-lg p-6 space-y-5">
            <h2 className="text-sm font-semibold text-[#F3F4F6]">
              2. Instalar en Egg Python Generic (Pelican / Pterodactyl)
            </h2>

            <ol className="space-y-4 text-sm text-[#D1D5DB] list-decimal list-inside">
              <li>
                Crea el servidor usando el Egg <span className="font-medium text-[#F3F4F6]">Python Generic (Pelican / Pterodactyl)</span> con <span className="font-mono text-[#60A5FA]">400% CPU</span>, <span className="font-mono text-[#60A5FA]">10240 MiB RAM</span> y <span className="font-mono text-[#60A5FA]">30720 MiB Disco</span>.
              </li>
              <li>
                Rellena tus credenciales en la pestaña <span className="font-medium text-[#F3F4F6]">Configurar .env</span>, marca incluir <code className="font-mono">.env</code> y descarga <code className="font-mono text-[#60A5FA]">hydrocompress-telegram-bot.zip</code>.
              </li>
              <li>
                Sube el ZIP en <span className="font-medium text-[#F3F4F6]">Archivos</span> de <span className="font-medium text-[#F3F4F6]">panel.plugged.host</span> y pulsa <span className="font-medium text-[#F3F4F6]">Descomprimir</span>.
              </li>
              <li>
                En <span className="font-medium text-[#F3F4F6]">Startup</span>, elige imagen <span className="font-medium text-[#F3F4F6]">Python 3.11</span> y cambia <code className="font-mono text-[#60A5FA]">App py file (PY_FILE)</code> a:
                <div className="mt-2 flex items-center justify-between gap-3 px-4 py-2.5 bg-[#0B111E] border border-[#1F2937] rounded-md font-mono text-xs text-[#F3F4F6]">
                  <code>bot.py</code>
                  <button
                    type="button"
                    onClick={() => handleCopy('cmd-start', 'bot.py')}
                    className="inline-flex items-center gap-1 text-[#9CA3AF] hover:text-[#F3F4F6] cursor-pointer"
                  >
                    {copiedId === 'cmd-start' ? (
                      <Check className="w-3.5 h-3.5 text-[#16A34A]" />
                    ) : (
                      <Copy className="w-3.5 h-3.5" />
                    )}
                    <span>{copiedId === 'cmd-start' ? 'Copiado' : 'Copiar'}</span>
                  </button>
                </div>
              </li>
              <li>
                Pulsa <span className="font-medium text-[#F3F4F6]">Start</span>: el bot descargará automáticamente <code className="font-mono">ffmpeg</code> y <code className="font-mono">ffprobe</code> estáticos en <code className="font-mono">./bin</code> sin permisos root.
              </li>
              <li>
                <span className="font-medium text-[#F3F4F6]">Entrar con tu cuenta de Telegram (opcional / Premium 4 GB):</span> escribe <code className="font-mono text-[#60A5FA]">/login</code> a tu bot en Telegram (envía teléfono, código separado por espacios y 2FA si aplica) o ejecuta <code className="font-mono">python3 login_account.py</code> en la consola.
              </li>
            </ol>
          </section>

          {/* Comandos del bot */}
          <section className="bg-[#111827] border border-[#1F2937] rounded-lg overflow-hidden">
            <div className="px-5 py-3 border-b border-[#1F2937]">
              <h2 className="text-sm font-semibold text-[#F3F4F6]">
                Comandos integrados en Telegram
              </h2>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-sm text-left">
                <thead className="border-b border-[#1F2937] text-xs text-[#9CA3AF]">
                  <tr>
                    <th className="px-5 py-2.5 font-medium">Comando</th>
                    <th className="px-5 py-2.5 font-medium">Rol</th>
                    <th className="px-5 py-2.5 font-medium">Acción</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-[#1F2937] text-xs">
                  {[
                    ['/start', 'Autorizados', 'Guía de uso: videos hasta 2 GB, enlaces HTTP(S), Muestra 15s, 10-bit, Trim y subs externos'],
                    ['/auto', 'Autorizados', 'Activar/desactivar Modo Lotes (encola videos automáticamente con tu ajuste guardado)'],
                    ['/queue', 'Autorizados', 'Ver trabajos comprimiendo, hilos activos y posición en espera'],
                    ['/setthumb', 'Autorizados', 'Responder a una foto para fijarla como miniatura personalizada'],
                    ['/delthumb', 'Autorizados', 'Volver a extracción automática de miniatura al 15% del video'],
                    ['/setwatermark <txt>', 'Autorizados', 'Guardar una marca de agua fija en tus videos comprimidos'],
                    ['/delwatermark', 'Autorizados', 'Eliminar tu marca de agua guardada'],
                    ['/stats', 'Autorizados', 'Total de videos comprimidos, enlaces >2 GB y espacio ahorrado'],
                    ['/admin', 'Dueño', 'CPU/RAM/Disco en vivo, alertas de error, logs, cuenta Telegram y concurrencia'],
                    ['/usage [reset]', 'Dueño', 'Consumo real de RAM, CPU y disco por video + recomendación de GB para el panel'],
                    ['/logs [n|errors|file]', 'Dueño', 'Ver últimas líneas/errores o descargar el archivo data/bot.log'],
                    ['/update', 'Dueño', 'Descargar la última versión desde GitHub y reiniciar automáticamente'],
                    ['/login', 'Dueño', 'Asistente seguro para iniciar sesión con tu cuenta Telegram (4 GB con Premium)'],
                    ['/logout', 'Dueño', 'Desconectar y eliminar la sesión de la cuenta Telegram'],
                    ['/setdump <id>', 'Dueño', 'Canal privado puente para entregar videos 2–4 GB (Premium) a cualquier usuario'],
                    ['/adduser <id>', 'Dueño', 'Autorizar un usuario en la lista blanca'],
                    ['/deluser <id>', 'Dueño', 'Revocar acceso de un usuario'],
                    ['/purge', 'Dueño', 'Limpiar carpetas y enlaces temporales inactivos'],
                    ['/cancelall', 'Dueño', 'Detener procesos FFmpeg activos y vaciar la cola'],
                  ].map(([cmd, role, desc]) => (
                    <tr key={cmd} className="hover:bg-[#161F31]">
                      <td className="px-5 py-2.5 font-mono text-[#60A5FA] whitespace-nowrap">{cmd}</td>
                      <td className="px-5 py-2.5 text-[#9CA3AF] whitespace-nowrap">{role}</td>
                      <td className="px-5 py-2.5 text-[#D1D5DB]">{desc}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        </main>
      )}
    </div>
  );
}
