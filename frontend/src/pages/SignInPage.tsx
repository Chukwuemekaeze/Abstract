import { SignIn } from '@clerk/clerk-react'

import { Card } from '@/components/ui/card'
import { clerkAppearance } from '@/lib/clerkAppearance'

export function SignInPage() {
  return (
    <div className="bg-background flex min-h-screen items-center justify-center p-4">
      <Card className="p-8">
        <SignIn
          routing="path"
          path="/sign-in"
          signUpUrl="/sign-up"
          fallbackRedirectUrl="/"
          appearance={clerkAppearance}
        />
      </Card>
    </div>
  )
}
