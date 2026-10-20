import { useAuth } from '~/composables/useAuth';

export default defineNuxtRouteMiddleware(async (to, from) => {
  const path = to.path.replace(/\/$/, '') || '/';
  if (path === '/login' || path.startsWith('/login/')) {
    return;
  }

  const auth = useAuth();
  try {
    const me = await auth.probe();
    if (!me) return navigateTo('/login');
    // OIDC can return directly to this page after setting the shared session cookie.
    if (!from.matched.length) auth.announceChange();
  } catch {
    if (from.matched.length) return abortNavigation();
  }
});
