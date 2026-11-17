export default defineAppConfig({
  ui: {
    primary: 'indigo',
    colors: {
      primary: 'indigo',
      secondary: 'amber',
      success: 'emerald',
      neutral: 'stone',
    },
    formField: {
      slots: {
        label: 'block font-medium text-default',
      },
    },
    empty: {
      variants: {
        variant: {
          outline: {
            root: 'border border-dashed border-default bg-muted/10 ring-0',
          },
        },
      },
    },
    input: {
      variants: {
        variant: {
          outline: 'bg-elevated/40 hover:bg-elevated/55 disabled:bg-elevated/20',
        },
      },
    },
    textarea: {
      variants: {
        variant: {
          outline: 'bg-elevated/40 hover:bg-elevated/55 disabled:bg-elevated/20',
        },
      },
    },
    table: {
      slots: {
        root: 'mw-table-surface',
        thead: 'bg-elevated/20',
        tbody: '[&>tr]:hover:bg-elevated/30',
        th: 'px-3 py-2 text-sm text-start font-medium text-toned',
        td: 'px-3 py-2 text-sm text-default align-top whitespace-normal wrap-anywhere',
        tfoot: 'bg-elevated/40 [&_th]:font-semibold [&_th]:text-highlighted',
      },
    },
    tooltip: {
      slots: {
        content: 'pointer-events-auto',
      },
    },
    toast: {
      slots: {
        root: 'shell-surface bg-transparent',
      },
    },
    modal: {
      slots: {
        content: 'shell-surface bg-transparent',
      },
    },
    slideover: {
      slots: {
        content: 'shell-surface bg-transparent',
      },
    },
    card: {
      slots: {
        root: 'min-w-0 max-w-full rounded-lg overflow-hidden',
      },
      variants: {
        variant: {
          outline: {
            root: 'border border-(--mw-surface-border-soft) bg-elevated/20 divide-y divide-default ring-0',
          },
          soft: {
            root: 'bg-elevated/20 divide-y divide-default',
          },
          subtle: {
            root: 'border border-(--mw-surface-border-soft) bg-elevated/20 divide-y divide-default ring-0',
          },
        },
      },
    },
    dropdownMenu: {
      slots: {
        content: 'mw-floating-surface rounded-lg bg-elevated/80 ring-0 dark:bg-elevated/30',
      },
    },
    popover: {
      slots: {
        content: 'mw-floating-surface rounded-lg bg-elevated/80 ring-0 dark:bg-elevated/30',
      },
    },
    inputMenu: {
      slots: {
        content: 'mw-floating-surface rounded-lg bg-elevated/80 ring-0 dark:bg-elevated/30',
      },
      variants: {
        variant: {
          outline: 'bg-elevated/40 hover:bg-elevated/55 disabled:bg-elevated/20',
        },
      },
    },
    select: {
      slots: {
        content: 'mw-floating-surface rounded-lg bg-elevated/80 ring-0 dark:bg-elevated/30',
      },
      variants: {
        variant: {
          outline: 'bg-elevated/40 hover:bg-elevated/55 disabled:bg-elevated/20',
        },
      },
    },
    selectMenu: {
      slots: {
        content:
          'mw-floating-surface min-w-40 rounded-lg bg-elevated/80 ring-0 dark:bg-elevated/30',
      },
      variants: {
        variant: {
          outline: 'bg-elevated/40 hover:bg-elevated/55 disabled:bg-elevated/20',
        },
      },
    },
    button: {
      compoundVariants: [
        {
          color: 'neutral',
          variant: 'outline',
          class:
            'bg-transparent hover:bg-elevated/30 active:bg-elevated/40 disabled:bg-transparent aria-disabled:bg-transparent',
        },
        {
          color: 'neutral',
          variant: 'soft',
          class:
            'bg-elevated/30 hover:bg-elevated/45 active:bg-elevated/55 disabled:bg-elevated/20 aria-disabled:bg-elevated/20',
        },
        {
          color: 'neutral',
          variant: 'ghost',
          class: 'hover:bg-elevated/30 active:bg-elevated/40 focus-visible:bg-elevated/30',
        },
      ],
      defaultVariants: {
        size: 'sm',
      },
    },
  },
});
