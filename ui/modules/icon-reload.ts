import { readFile } from 'node:fs/promises';
import { extname, isAbsolute, relative, resolve, sep } from 'node:path';
import { addVitePlugin, defineNuxtModule, updateTemplates, useLogger } from '@nuxt/kit';
import { IconUsageScanner } from '@nuxt/icon/utils';

const ICON_TEMPLATE = /^nuxt-icon-client-bundle(?:$|[._-])/;
const SOURCE_EXTENSIONS = new Set(['.js', '.ts', '.vue']);

export const isRelevantSource = (path: string, rootDir: string): boolean => {
  const absolutePath = resolve(rootDir, path);
  const appDirectory = resolve(rootDir, 'app');
  const relativePath = relative(appDirectory, absolutePath);

  return (
    '' !== relativePath &&
    !isAbsolute(relativePath) &&
    relativePath !== '..' &&
    !relativePath.startsWith(`..${sep}`) &&
    SOURCE_EXTENSIONS.has(extname(absolutePath))
  );
};

export const extractIcons = (
  scanner: IconUsageScanner,
  code: string,
  icons: Set<string>,
): boolean => {
  const count = icons.size;
  scanner.extractFromCode(code, icons);
  return icons.size !== count;
};

export default defineNuxtModule({
  meta: { name: 'mw-icon-reload' },
  setup(_options, nuxt) {
    if (!nuxt.options.dev) {
      return;
    }

    const iconOptions = nuxt.options.icon;
    const scanOptions = iconOptions && iconOptions.clientBundle?.scan;
    if (!scanOptions) {
      return;
    }

    const scanner = new IconUsageScanner(scanOptions);
    const incrementalIcons = new Set<string>();

    nuxt.hook('icon:clientBundleIcons', (icons) => {
      for (const icon of icons) {
        incrementalIcons.add(icon);
      }
      for (const icon of incrementalIcons) {
        icons.add(icon);
      }
    });

    const logger = useLogger('mw-icon-reload');
    const appDirectory = resolve(nuxt.options.rootDir, 'app');

    addVitePlugin({
      name: 'mw-icon-reload-hmr',
      configureServer(server) {
        let updateTimer: ReturnType<typeof setTimeout> | undefined;
        let updating = false;
        let pendingUpdate = false;
        const pendingSourcePaths = new Set<string>();

        const regenerate = async (): Promise<void> => {
          if (updating) {
            pendingUpdate = true;
            return;
          }

          updating = true;
          try {
            do {
              pendingUpdate = false;
              const destinations = new Set<string>();
              try {
                let iconsChanged = false;
                const sourcePaths = [...pendingSourcePaths];
                pendingSourcePaths.clear();
                await Promise.all(
                  sourcePaths.map(async (path) => {
                    const code = await readFile(path, 'utf8').catch(
                      (error: NodeJS.ErrnoException) => {
                        if ('ENOENT' === error.code) {
                          return '';
                        }
                        throw error;
                      },
                    );
                    iconsChanged = extractIcons(scanner, code, incrementalIcons) || iconsChanged;
                  }),
                );

                if (!iconsChanged) {
                  continue;
                }

                await updateTemplates({
                  filter: (template) => {
                    if (!ICON_TEMPLATE.test(template.filename)) {
                      return false;
                    }
                    destinations.add(template.dst);
                    return true;
                  },
                });

                const modules = [...destinations].flatMap((destination) => [
                  ...(server.environments.client.moduleGraph.getModulesByFile(destination) ?? []),
                ]);
                await Promise.all(
                  modules.map((module) => {
                    server.environments.client.moduleGraph.invalidateModule(module);
                    return server.environments.client.reloadModule(module);
                  }),
                );
              } catch (error) {
                logger.error(error);
              }
            } while (pendingUpdate);
          } finally {
            updating = false;
          }
        };

        const scheduleUpdate = (path: string): void => {
          if (!isRelevantSource(path, nuxt.options.rootDir)) {
            return;
          }

          pendingSourcePaths.add(resolve(nuxt.options.rootDir, path));

          if (updateTimer) {
            clearTimeout(updateTimer);
          }
          updateTimer = setTimeout(() => void regenerate(), 50);
        };

        server.watcher.add(appDirectory);
        server.watcher.on('add', scheduleUpdate);
        server.watcher.on('change', scheduleUpdate);
        server.watcher.on('unlink', scheduleUpdate);
        server.httpServer?.once('close', () => {
          if (updateTimer) {
            clearTimeout(updateTimer);
          }
          server.watcher.off('add', scheduleUpdate);
          server.watcher.off('change', scheduleUpdate);
          server.watcher.off('unlink', scheduleUpdate);
        });
      },
    });
  },
});
