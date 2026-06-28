import type { Appearance } from '@clerk/shared/types'

// Aligns Clerk's components with the shadcn theme by mapping Clerk's design tokens
// onto the same CSS variables the rest of the UI uses (see index.css). Element
// overrides nudge the primary button and inputs to match shadcn shapes.
export const clerkAppearance: Appearance = {
  variables: {
    colorPrimary: 'var(--primary)',
    colorBackground: 'var(--card)',
    colorText: 'var(--card-foreground)',
    colorTextSecondary: 'var(--muted-foreground)',
    colorInputBackground: 'var(--background)',
    colorInputText: 'var(--foreground)',
    colorDanger: 'var(--destructive)',
    borderRadius: 'var(--radius)',
    fontFamily: 'inherit',
  },
  elements: {
    card: 'shadow-none bg-transparent',
    rootBox: 'w-full',
    formButtonPrimary:
      'bg-primary text-primary-foreground hover:bg-primary/90 normal-case',
    footerActionLink: 'text-primary hover:text-primary/90',
  },
}
