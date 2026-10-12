"use client";

import { Fragment, ReactNode, useEffect, useState } from "react";
import { ArrowSquareOut, Check, Copy, ExclamationMark, WarningCircle } from "@phosphor-icons/react";
import type { UpdateStatus } from "../../../lib/api";
import { formatTime } from "../../../lib/tasks";
import { cn, fmt } from "../../../lib/utils";
import { Sheet } from "../../../components/ui/sheet";
import { useConfirm } from "../../../components/ui/confirm";
import { Spinner } from "../../../components/ui/controls";
import { ListRow, ListSection } from "../../../components/ui/list";
import { BrandMark } from "../../../components/ui/brand";
import { UpdatePhase, useSelfUpdate } from "../../../components/app/SelfUpdate";

type T = (key: string) => string;

const UPDATE_COMMAND = "docker compose pull && docker compose up -d";
const SOCKET_LINE = "- /var/run/docker.sock:/var/run/docker.sock";
const REBUILD_COMMAND = "git pull && docker compose up -d --build";

const IN_PROGRESS: UpdatePhase[] = ["pulling", "restarting", "done"];

/** 设置页「关于」里这一行右侧的状态 */
export function UpdateRowValue({ t }: { t: T }) {
  const { status, checking, checkFailed, phase, updateAvailable } = useSelfUpdate();
  if (IN_PROGRESS.includes(phase)) {
    return (
      <span className="inline-flex items-center gap-1.5">
        <Spinner className="h-3.5 w-3.5 border-[1.5px]" />
        {t("update_in_progress_short")}
      </span>
    );
  }
  if (!status) {
    if (checking) return <Spinner className="h-3.5 w-3.5 border-[1.5px]" label={t("update_checking")} />;
    return checkFailed ? <>{t("update_check_failed")}</> : null;
  }
  if (updateAvailable) {
    return (
      <span className="chip chip-accent">
        <i aria-hidden />
        {t("update_available_short")}
      </span>
    );
  }
  if (status.update_available === false) return <>{t("update_latest_short")}</>;
  return null;
}

function CodeLine({ code, t }: { code: string; t: T }) {
  const [copied, setCopied] = useState(false);
  const parts = code.split(/(?<=&& |:)/);
  useEffect(() => {
    if (!copied) return;
    const timer = setTimeout(() => setCopied(false), 1600);
    return () => clearTimeout(timer);
  }, [copied]);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
    } catch {}
  };
  return (
    <div className="list-row gap-2 py-2.5">
      <code className="min-w-0 flex-1 overflow-x-auto font-mono text-[13px] leading-[19px] text-label">
        {/* 优先在 "&& " 和 ":" 之后换行；一段放不下时才在段内空格处换行 */}
        {parts.map((part, index) => (
          <Fragment key={index}>
            {index > 0 ? parts[index - 1].endsWith(" ") ? " " : <wbr /> : null}
            <span className="inline-block max-w-full">
              {/* 连字符处也不断开（-d、--build）；YAML 列表的 "- " 与路径连在一起 */}
              {(part.startsWith("- ") ? [part.trim()] : part.trim().split(" ")).map((word, i) => (
                <Fragment key={i}>
                  {i > 0 ? " " : null}
                  <span className="whitespace-nowrap">{word}</span>
                </Fragment>
              ))}
            </span>
          </Fragment>
        ))}
      </code>
      <button type="button" onClick={copy} className="btn btn-sm btn-tinted flex-none" aria-live="polite">
        {copied ? <Check size={14} weight="bold" aria-hidden /> : <Copy size={14} weight="bold" aria-hidden />}
        {/* 很窄的屏幕只留图标，把宽度让给命令 */}
        <span className="max-[359px]:sr-only">{copied ? t("copied") : t("copy")}</span>
      </button>
    </div>
  );
}

type StepState = "wait" | "run" | "up" | "down";

function Step({ state, title, note }: { state: StepState; title: string; note?: ReactNode }) {
  return (
    <li className="flex min-h-[44px] items-center gap-3 px-4 py-2">
      <span className={cn("st", `is-${state}`)} aria-hidden>
        {state === "up" ? <Check size={10} weight="bold" /> : null}
        {state === "down" ? <ExclamationMark size={10} weight="bold" /> : null}
      </span>
      <span className="min-w-0 flex-1">
        <span className={cn("block text-body", state === "wait" && "text-label-2")}>{title}</span>
        {note ? <span className="mt-0.5 block text-footnote text-label-2">{note}</span> : null}
      </span>
    </li>
  );
}

function Progress({ phase, detail, pullDenied, t }: { phase: UpdatePhase; detail: string | null; pullDenied: boolean; t: T }) {
  const failedAt = phase === "failed" ? 0 : phase === "rolled_back" || phase === "timeout" ? 1 : -1;
  const reached = ({ pulling: 0, restarting: 1, done: 3 } as Record<string, number>)[phase] ?? failedAt;
  const stateOf = (index: number): StepState => {
    if (index === failedAt) return "down";
    if (index < reached) return "up";
    if (index === reached && failedAt < 0) return "run";
    return "wait";
  };
  const restartNote =
    phase === "rolled_back" ? (
      <>
        {t("update_rolled_back_desc")}
        {detail ? <span className="mt-0.5 block text-label-3">{detail}</span> : null}
      </>
    ) : phase === "timeout" ? (
      t("update_timeout_desc")
    ) : phase === "restarting" ? (
      t("update_restarting_desc")
    ) : undefined;
  return (
    <ListSection>
      <ol aria-live="polite" className="py-1.5">
        <Step
          state={stateOf(0)}
          title={t("update_step_pull")}
          note={phase === "failed" ? detail || t(pullDenied ? "update_pull_denied" : "update_start_failed") : phase === "pulling" ? detail : undefined}
        />
        <Step state={stateOf(1)} title={t("update_step_restart")} note={restartNote} />
        <Step
          state={stateOf(2)}
          title={t("update_step_done")}
          note={phase === "done" ? t("update_reloading") : undefined}
        />
      </ol>
    </ListSection>
  );
}

const BLOCK_REASON_KEY: Record<string, string> = {
  disabled: "update_reason_disabled",
  no_socket: "update_reason_no_socket",
  permission: "update_reason_permission",
  unreachable: "update_reason_unreachable",
  not_container: "update_reason_not_container",
  pinned: "update_reason_pinned",
  not_pullable: "update_reason_not_pullable",
};

function ManualGuide({ status, t }: { status: UpdateStatus; t: T }) {
  const reason = status.docker.reason || "no_socket";
  const canEnable = reason === "no_socket" || reason === "permission" || reason === "unreachable";
  return (
    <>
      <ListSection header={t("update_manual_title")} footer={t(BLOCK_REASON_KEY[reason] || "update_reason_no_socket")}>
        {reason === "not_container" ? (
          <p className="px-4 py-3 text-subhead text-label-2">{t("update_manual_not_container")}</p>
        ) : reason === "not_pullable" ? (
          <>
            <p className="px-4 pb-1 pt-3 text-subhead text-label-2">{t("update_manual_rebuild_desc")}</p>
            <CodeLine code={REBUILD_COMMAND} t={t} />
          </>
        ) : (
          <>
            <p className="px-4 pb-1 pt-3 text-subhead text-label-2">{t("update_manual_desc")}</p>
            <CodeLine code={UPDATE_COMMAND} t={t} />
          </>
        )}
      </ListSection>
      {canEnable ? (
        <ListSection header={t("update_enable_title")} footer={t("update_enable_risk")}>
          <p className="px-4 pb-1 pt-3 text-subhead text-label-2">{t("update_enable_desc")}</p>
          <CodeLine code={SOCKET_LINE} t={t} />
        </ListSection>
      ) : null}
    </>
  );
}

export function UpdateSheet({ open, t, language, onClose }: { open: boolean; t: T; language: string; onClose: () => void }) {
  const { status, checking, checkFailed, phase, detail, updateAvailable, check, start, reset } = useSelfUpdate();
  const confirm = useConfirm();
  const busy = IN_PROGRESS.includes(phase);

  // 每次打开都重新问一次服务端（不强制联网检查）
  useEffect(() => {
    if (open) check(false);
  }, [open, check]);

  const close = () => {
    reset();
    onClose();
  };

  const recheck = () => {
    reset();
    check(true);
  };

  const current = status?.current;
  const oneClick = Boolean(status?.docker.enabled);
  // 已是最新时不算进度，直接显示检查结果
  const showProgress = phase !== "idle" && phase !== "up_to_date";

  const confirmStart = async () => {
    const ok = await confirm({
      title: t("update_confirm_title"),
      message: t("update_confirm_message"),
      confirmText: t("update_now"),
    });
    if (ok) start();
  };

  let footer: ReactNode = null;
  if (busy) {
    footer = (
      <button type="button" className="btn btn-lg btn-primary w-full" disabled>
        <Spinner className="text-white" />
        {phase === "done" ? t("update_reloading") : t("update_in_progress_short")}
      </button>
    );
  } else if (phase === "up_to_date") {
    footer = (
      <button type="button" className="btn btn-lg btn-gray w-full" onClick={close}>
        {t("done")}
      </button>
    );
  } else if (phase === "timeout") {
    footer = (
      <button type="button" className="btn btn-lg btn-primary w-full" onClick={() => window.location.reload()}>
        {t("update_reload_page")}
      </button>
    );
  } else if (oneClick && status && status.update_available !== false && phase === "idle") {
    footer = (
      <button type="button" className="btn btn-lg btn-primary w-full" onClick={confirmStart}>
        {status.update_available ? t("update_now") : t("update_try")}
      </button>
    );
  } else if (status || checkFailed) {
    footer = (
      <button type="button" className="btn btn-lg btn-gray w-full" onClick={recheck} disabled={checking}>
        {checking ? <Spinner /> : null}
        {t("update_recheck")}
      </button>
    );
  }

  const latest = status?.latest_version;
  let headline: ReactNode = null;
  if (status && !showProgress) {
    if (updateAvailable) {
      headline = (
        <>
          <span className="block text-headline text-accent-text">{t("update_available_title")}</span>
          <span className="mt-0.5 block text-footnote text-label-2">
            {latest && latest !== current?.version ? fmt(t("update_latest_version"), { version: latest }) : t("update_new_build")}
          </span>
        </>
      );
    } else if (status.update_available === false) {
      headline = <span className="block text-headline">{t("update_latest_title")}</span>;
    } else {
      headline = (
        <>
          <span className="block text-headline">{t("update_unknown_title")}</span>
          <span className="mt-0.5 block text-footnote text-label-2">
            {status.check_enabled ? t("update_unknown_desc") : t("update_check_disabled")}
          </span>
        </>
      );
    }
  }

  return (
    <Sheet
      open={open}
      onClose={close}
      title={t("software_update")}
      size="sm"
      footer={footer}
    >
      <div className="space-y-6 pt-1">
        <div className="flex items-center gap-3.5 px-1">
          <span className="flex-none rounded-[14px] bg-[var(--field-neutral)]">
            <BrandMark size={52} className="rounded-[14px]" />
          </span>
          <div className="min-w-0 flex-1">
            <p className="text-headline">TG SignPulse</p>
            <p className="num mt-0.5 truncate text-footnote text-label-2">
              {current ? `v${current.version}` : "—"}
            </p>
            {current?.built_at ? (
              <p className="num truncate text-footnote text-label-3">
                {t("built_at")} {current.built_at}
              </p>
            ) : null}
          </div>
        </div>

        {!status && checking ? (
          <div className="flex justify-center py-6">
            <Spinner label={t("update_checking")} />
          </div>
        ) : null}

        {!status && !checking && checkFailed ? (
          <div role="alert" className="flex gap-2.5 rounded-group bg-danger-soft px-4 py-3 text-subhead">
            <WarningCircle size={20} weight="fill" className="mt-px flex-none text-danger-dot" aria-hidden />
            {t("update_check_failed_desc")}
          </div>
        ) : null}

        {showProgress ? (
          <Progress phase={phase} detail={detail} pullDenied={status?.docker.reason === "not_pullable"} t={t} />
        ) : null}

        {headline ? (
          <ListSection
            footer={
              status?.checked_at
                ? fmt(t("update_checked_at"), { time: formatTime(status.checked_at, language) })
                : undefined
            }
          >
            <div className="list-row py-3">
              <span className="min-w-0 flex-1">{headline}</span>
              {checking ? <Spinner className="h-4 w-4 border-[1.5px]" /> : null}
            </div>
            {status?.changes_url && (updateAvailable || status.update_available === null) ? (
              <ListRow
                title={t("update_view_changes")}
                tone="accent"
                href={status.changes_url}
                external
                accessory={<ArrowSquareOut size={16} className="flex-none text-label-3" aria-hidden />}
              />
            ) : null}
          </ListSection>
        ) : null}

        {status && oneClick && !showProgress && status.update_available !== false ? (
          <p className="px-4 text-footnote text-label-2">
            {t("update_restart_hint")}
            {status.local_build ? ` ${t("update_local_build_hint")}` : ""}
          </p>
        ) : null}

        {status && !oneClick && (!showProgress || phase === "failed") ? <ManualGuide status={status} t={t} /> : null}
      </div>
    </Sheet>
  );
}
