import math
with(open('prime.txt','r+')) as f:
    primeSet = f.read().splitlines()
    primeSetInt = [int(i) for i in primeSet]
    del primeSet

    curNum = primeSetInt[-1]
    print("last number recorded was: " + str(curNum))
    while True:
        curNum += 2
        print("currently testing: " + str(curNum))
        isPrime = True
        for i in primeSetInt:
            if math.sqrt(curNum) == math.floor(math.sqrt(curNum)):
                isPrime == False
                break
            if i > math.floor(math.sqrt(curNum)):
                break
            if curNum % 5 == 0 or curNum % i == 0:
                isPrime = False
                print("this number is not prime: " + str(curNum))
                break
        if isPrime:
            f.write(str(curNum) + '\n')
            primeSetInt.append(curNum)
            print("this number is prime: " + str(curNum))

            print("size of set is now: " + str(len(primeSetInt)))
