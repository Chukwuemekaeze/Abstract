import { SignUp } from '@clerk/clerk-react'

import { Card } from '@/components/ui/card'
import { clerkAppearance } from '@/lib/clerkAppearance'

export function SignUpPage() {
  return (
    <div className="bg-background flex min-h-screen items-center justify-center p-4">
      <Card className="p-8">
        <SignUp
          routing="path"
          path="/sign-up"
          signInUrl="/sign-in"
          fallbackRedirectUrl="/"
          appearance={clerkAppearance}
        />
      </Card>
    </div>
  )
}
