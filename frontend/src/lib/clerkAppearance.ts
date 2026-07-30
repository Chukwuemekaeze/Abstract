import type { Appearance } from '@clerk/shared/types'
import { dark } from '@clerk/themes'

// Aligns Clerk's components with the shadcn theme. The dark base theme keeps all
// popover text, icons, and menu items readable on our dark surface; the variables
// below then map Clerk's tokens onto the same CSS variables the rest of the UI
// uses (see index.css), and the element overrides match shadcn shapes.
export const clerkAppearance: Appearance = {
  baseTheme: dark,
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
